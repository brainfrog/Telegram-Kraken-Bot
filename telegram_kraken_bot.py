#!/usr/bin/python3

import json
import os
import sys
import time
import threading
import requests
import kraken_api
import re

from enum import Enum, auto
from telegram import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, ParseMode
from telegram.ext import Updater, CommandHandler, ConversationHandler, RegexHandler, MessageHandler
from telegram.ext.filters import Filters
from utils import *
from file_logger import logger

# Check if file 'config.json' exists. Exit if not.
if os.path.isfile("config.json"):
    # Read configuration
    with open("config.json") as config_file:
        config = json.load(config_file)
else:
    exit("No configuration file 'config.json' found")

# Set up logging
logger.init(config["log_level"], config["log_to_file"])

# Set bot token, get dispatcher and job queue
updater = Updater(token=config["bot_token"])
dispatcher = updater.dispatcher
job_queue = updater.job_queue

# Connect to kraken
kraken = kraken_api.Kraken("kraken.key", config["retries"])

# Cached objects
# All open orders
orders = list()
# All assets with internal long name & external short name
assets = dict()
# All assets from config with their trading pair
pairs = dict()
# Minimum order limits for assets
limits = dict()


class TradeState(Enum):
    CURRENCY = auto()
    BUY_SELL = auto()
    ORDER_TYPE = auto()
    PRICE = auto()
    STOP_PRICE = auto()
    VOLUME = auto()
    CONFIRM = auto()


# Enum for workflow handler
class WorkflowEnum(Enum):
    TRADE_BUY_SELL = auto()
    TRADE_CURRENCY = auto()
    TRADE_SELL_ALL_CONFIRM = auto()
    TRADE_PRICE = auto()
    TRADE_VOL_TYPE = auto()
    TRADE_VOLUME = auto()
    TRADE_VOLUME_ASSET = auto()
    TRADE_CONFIRM = auto()
    ORDERS_CLOSE = auto()
    ORDERS_CLOSE_ORDER = auto()
    BOT_SUB_CMD = auto()
    SETTINGS_CHANGE = auto()
    SETTINGS_SAVE = auto()
    SETTINGS_CONFIRM = auto()


# Enum for keyboard buttons
class KeyboardEnum(Enum):
    BUY = auto()
    SELL = auto()
    VOLUME = auto()
    ALL = auto()
    YES = auto()
    NO = auto()
    CANCEL = auto()
    CLOSE_ORDER = auto()
    CLOSE_ALL = auto()
    RESTART = auto()
    SHUTDOWN = auto()
    SETTINGS = auto()
    API_STATE = auto()
    MARKET_PRICE = auto()

    def clean(self):
        return self.name.replace("_", " ")


# Decorator to restrict access if user is not the same as in config
def restrict_access(func):
    def _restrict_access(bot, update):
        chat_id = get_chat_id(update)
        if str(chat_id) != config["user_id"]:
            if config["show_access_denied"]:
                # Inform user who tried to access
                bot.send_message(chat_id, text="Access denied")

                # Inform owner of bot
                msg = "Access denied for user %s" % chat_id
                bot.send_message(config["user_id"], text=msg)

                logger.warning(msg)
            return
        else:
            return func(bot, update)
    return _restrict_access


# Get balance of all currencies
@restrict_access
def balance_cmd(bot, update):
    update.message.reply_text(emo_wa + " Retrieving balance...")

    msg = get_api_result(kraken.balance(), update)
    if not msg:
        return

    update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


# Create orders to buy or sell currencies with price limit - choose 'buy' or 'sell'
@restrict_access
def trade_cmd(bot, update):
    reply_msg = "Buy or sell?"

    buttons = [
        KeyboardButton(KeyboardEnum.BUY.clean()),
        KeyboardButton(KeyboardEnum.SELL.clean())
    ]

    cancel_btn = [KeyboardButton(KeyboardEnum.CANCEL.clean())]

    menu = build_menu(buttons, n_cols=2, footer_buttons=cancel_btn)
    reply_mrk = ReplyKeyboardMarkup(menu, resize_keyboard=True)
    update.message.reply_text(reply_msg, reply_markup=reply_mrk)

    return WorkflowEnum.TRADE_BUY_SELL


# Save if BUY or SELL order and choose the currency to trade
def trade_buy_sell(bot, update, chat_data):
    chat_data["buysell"] = update.message.text.lower()

    reply_msg = "Choose currency"

    cancel_btn = [KeyboardButton(KeyboardEnum.CANCEL.clean())]

    # If SELL chosen, then include button 'ALL' to sell everything
    if chat_data["buysell"].upper() == KeyboardEnum.SELL.clean():
        cancel_btn.insert(0, KeyboardButton(KeyboardEnum.ALL.clean()))

    menu = build_menu(coin_buttons(), n_cols=3, footer_buttons=cancel_btn)
    reply_mrk = ReplyKeyboardMarkup(menu, resize_keyboard=True)
    update.message.reply_text(reply_msg, reply_markup=reply_mrk)

    return WorkflowEnum.TRADE_CURRENCY


# Show confirmation to sell all assets
def trade_sell_all(bot, update):
    msg = " Sell " + bold("all") + " assets to current market price? All open orders will be closed!"
    update.message.reply_text(emo_qu + msg, reply_markup=keyboard_confirm(), parse_mode=ParseMode.MARKDOWN)

    return WorkflowEnum.TRADE_SELL_ALL_CONFIRM


# Sells all assets for there respective current market value
def trade_sell_all_confirm(bot, update):
    if update.message.text.upper() == KeyboardEnum.NO.clean():
        return cancel(bot, update)

    update.message.reply_text(emo_wa + " Preparing to sell everything...")

    # Send request for open orders to Kraken
    res_open_orders = kraken.query("OpenOrders", private=True)

    # If Kraken replied with an error, show it
    if handle_api_error(res_open_orders, update):
        return

    # Close all currently open orders
    if res_open_orders["result"]["open"]:
        for order in res_open_orders["result"]["open"]:
            req_data = dict()
            req_data["txid"] = order

            # Send request to Kraken to cancel orders
            res_open_orders = kraken.query("CancelOrder", data=req_data, private=True)

            # If Kraken replied with an error, show it
            if handle_api_error(res_open_orders, update, "Not possible to close order\n" + order + "\n"):
                return

    # Send request to Kraken to get current balance of all assets
    res_balance = kraken.query("Balance", private=True)

    # If Kraken replied with an error, show it
    if handle_api_error(res_balance, update):
        return

    # Go over all assets and sell them
    for balance_asset, amount in res_balance["result"].items():
        # Asset is fiat-currency and not crypto-currency - skip it
        if balance_asset.startswith("Z"):
            continue

        # Filter out 0 volume currencies
        if amount == "0.0000000000":
            continue

        # Get clean asset name
        balance_asset = assets[balance_asset]["altname"]

        # Make sure that the order size is at least the minimum order limit
        if balance_asset in limits:
            if float(amount) < float(limits[balance_asset]):
                msg_error = emo_er + " Volume to low. Must be > " + limits[balance_asset]
                msg_next = emo_wa + " Selling next asset..."

                update.message.reply_text(msg_error + "\n" + msg_next)
                logger.warning(msg_error)
                continue
        else:
            logger.warning("No minimum order limit in config for coin " + balance_asset)
            continue

        req_data = dict()
        req_data["type"] = "sell"
        req_data["trading_agreement"] = "agree"
        req_data["pair"] = pairs[balance_asset]
        req_data["ordertype"] = "market"
        req_data["volume"] = amount

        # Send request to create order to Kraken
        res_add_order = kraken.query("AddOrder", data=req_data, private=True)

        # If Kraken replied with an error, show it
        if handle_api_error(res_add_order, update):
            continue

        order_txid = res_add_order["result"]["txid"][0]

        # Add Job to JobQueue to check status of created order (if setting is enabled)
        if config["check_trade"]:
            trade_time = config["check_trade_time"]
            context = dict(order_txid=order_txid)
            job_queue.run_repeating(order_state_check, trade_time, context=context)

    msg = emo_fi + " Created orders to sell all assets"
    update.message.reply_text(bold(msg), reply_markup=keyboard_cmds(), parse_mode=ParseMode.MARKDOWN)

    return ConversationHandler.END


# Save currency to trade and enter price per unit to trade
def trade_currency(bot, update, chat_data):
    chat_data["currency"] = update.message.text.upper()

    asset_one, asset_two = assets_from_pair(pairs[chat_data["currency"]])
    chat_data["one"] = asset_one
    chat_data["two"] = asset_two

    button = [KeyboardButton(KeyboardEnum.MARKET_PRICE.clean())]
    cancel_btn = [KeyboardButton(KeyboardEnum.CANCEL.clean())]
    reply_mrk = ReplyKeyboardMarkup(build_menu(button, footer_buttons=cancel_btn), resize_keyboard=True)

    reply_msg = "Enter price per coin in " + bold(assets[chat_data["two"]]["altname"])
    update.message.reply_text(reply_msg, reply_markup=reply_mrk, parse_mode=ParseMode.MARKDOWN)
    return WorkflowEnum.TRADE_PRICE


# Save price per unit and choose how to enter the
# trade volume (fiat currency, volume or all available funds)
def trade_price(bot, update, chat_data):
    # Check if key 'market_price' already exists. Yes means that we
    # already saved the values and we only need to enter the volume again
    if "market_price" not in chat_data:
        if update.message.text.upper() == KeyboardEnum.MARKET_PRICE.clean():
            chat_data["market_price"] = True
        else:
            chat_data["market_price"] = False
            chat_data["price"] = update.message.text.upper().replace(",", ".")

    reply_msg = "How to enter the volume?"

    # If price is 'MARKET PRICE' and it's a buy-order, don't show options
    # how to enter volume since there is only one way to do it
    if chat_data["market_price"] and chat_data["buysell"] == "buy":
        cancel_btn = build_menu([KeyboardButton(KeyboardEnum.CANCEL.clean())])
        reply_mrk = ReplyKeyboardMarkup(cancel_btn, resize_keyboard=True)
        update.message.reply_text("Enter volume", reply_markup=reply_mrk)
        chat_data["vol_type"] = KeyboardEnum.VOLUME.clean()
        return WorkflowEnum.TRADE_VOLUME

    elif chat_data["market_price"] and chat_data["buysell"] == "sell":
        buttons = [
            KeyboardButton(KeyboardEnum.ALL.clean()),
            KeyboardButton(KeyboardEnum.VOLUME.clean())
        ]
        cancel_btn = [KeyboardButton(KeyboardEnum.CANCEL.clean())]
        cancel_btn = build_menu(buttons, n_cols=2, footer_buttons=cancel_btn)
        reply_mrk = ReplyKeyboardMarkup(cancel_btn, resize_keyboard=True)

    else:
        buttons = [
            KeyboardButton(assets[chat_data["two"]]["altname"]),
            KeyboardButton(KeyboardEnum.VOLUME.clean()),
            KeyboardButton(KeyboardEnum.ALL.clean())
        ]
        cancel_btn = [KeyboardButton(KeyboardEnum.CANCEL.clean())]
        cancel_btn = build_menu(buttons, n_cols=3, footer_buttons=cancel_btn)
        reply_mrk = ReplyKeyboardMarkup(cancel_btn, resize_keyboard=True)

    update.message.reply_text(reply_msg, reply_markup=reply_mrk)
    return WorkflowEnum.TRADE_VOL_TYPE


# Save volume type decision and enter volume
def trade_vol_asset(bot, update, chat_data):
    # Check if correct currency entered
    if chat_data["two"].endswith(update.message.text.upper()):
        chat_data["vol_type"] = update.message.text.upper()
    else:
        update.message.reply_text(emo_er + " Entered volume type not valid")
        return WorkflowEnum.TRADE_VOL_TYPE

    reply_msg = "Enter volume in " + bold(chat_data["vol_type"])

    cancel_btn = build_menu([KeyboardButton(KeyboardEnum.CANCEL.clean())])
    reply_mrk = ReplyKeyboardMarkup(cancel_btn, resize_keyboard=True)
    update.message.reply_text(reply_msg, reply_markup=reply_mrk, parse_mode=ParseMode.MARKDOWN)

    return WorkflowEnum.TRADE_VOLUME_ASSET


# Volume type 'VOLUME' chosen - meaning that
# you can enter the volume directly
def trade_vol_volume(bot, update, chat_data):
    chat_data["vol_type"] = update.message.text.upper()

    reply_msg = "Enter volume"

    cancel_btn = build_menu([KeyboardButton(KeyboardEnum.CANCEL.clean())])
    reply_mrk = ReplyKeyboardMarkup(cancel_btn, resize_keyboard=True)
    update.message.reply_text(reply_msg, reply_markup=reply_mrk)

    return WorkflowEnum.TRADE_VOLUME


# Volume type 'ALL' chosen - meaning that
# all available funds will be used
def trade_vol_all(bot, update, chat_data):
    update.message.reply_text(emo_wa + " Calculating volume...")

    # Send request to Kraken to get current balance of all currencies
    res_balance = kraken.query("Balance", private=True)

    # If Kraken replied with an error, show it
    if handle_api_error(res_balance, update):
        return

    # Send request to Kraken to get open orders
    res_orders = kraken.query("OpenOrders", private=True)

    # If Kraken replied with an error, show it
    if handle_api_error(res_orders, update):
        return

    # BUY -----------------
    if chat_data["buysell"].upper() == KeyboardEnum.BUY.clean():
        # Get amount of available currency to buy from
        avail_buy_from_cur = float(res_balance["result"][chat_data["two"]])

        # Go through all open orders and check if buy-orders exist
        # If yes, subtract their value from the total of currency to buy from
        if res_orders["result"]["open"]:
            for order in res_orders["result"]["open"]:
                order_desc = res_orders["result"]["open"][order]["descr"]["order"]
                order_desc_list = order_desc.split(" ")
                coin_price = trim_zeros(order_desc_list[5])
                order_volume = order_desc_list[1]
                order_type = order_desc_list[0]

                if order_type == "buy":
                    avail_buy_from_cur = float(avail_buy_from_cur) - (float(order_volume) * float(coin_price))

        # Calculate volume depending on available trade-to balance and round it to 8 digits
        chat_data["volume"] = "{0:.8f}".format(avail_buy_from_cur / float(chat_data["price"]))

        # If available volume is 0, return without creating an order
        if chat_data["volume"] == "0.00000000":
            msg = emo_er + " Available " + assets[chat_data["two"]]["altname"] + " volume is 0"
            update.message.reply_text(msg, reply_markup=keyboard_cmds())
            return ConversationHandler.END
        else:
            trade_show_conf(update, chat_data)

    # SELL -----------------
    if chat_data["buysell"].upper() == KeyboardEnum.SELL.clean():
        available_volume = res_balance["result"][chat_data["one"]]

        # Go through all open orders and check if sell-orders exists for the currency
        # If yes, subtract their volume from the available volume
        if res_orders["result"]["open"]:
            for order in res_orders["result"]["open"]:
                order_desc = res_orders["result"]["open"][order]["descr"]["order"]
                order_desc_list = order_desc.split(" ")

                # Get the currency of the order
                for asset, data in assets.items():
                    if order_desc_list[2].endswith(data["altname"]):
                        order_currency = order_desc_list[2][:-len(data["altname"])]
                        break

                order_volume = order_desc_list[1]
                order_type = order_desc_list[0]

                # Check if currency from oder is the same as currency to sell
                if chat_data["currency"] in order_currency:
                    if order_type == "sell":
                        available_volume = str(float(available_volume) - float(order_volume))

        # Get volume from balance and round it to 8 digits
        chat_data["volume"] = "{0:.8f}".format(float(available_volume))

        # If available volume is 0, return without creating an order
        if chat_data["volume"] == "0.00000000":
            msg = emo_er + " Available " + chat_data["currency"] + " volume is 0"
            update.message.reply_text(msg, reply_markup=keyboard_cmds())
            return ConversationHandler.END
        else:
            trade_show_conf(update, chat_data)

    return WorkflowEnum.TRADE_CONFIRM


# Calculate the volume depending on entered volume type currency
def trade_volume_asset(bot, update, chat_data):
    amount = float(update.message.text.replace(",", "."))
    price_per_unit = float(chat_data["price"])
    chat_data["volume"] = "{0:.8f}".format(amount / price_per_unit)

    # Make sure that the order size is at least the minimum order limit
    if chat_data["currency"] in limits:
        if float(chat_data["volume"]) < float(limits[chat_data["currency"]]):
            msg_error = emo_er + " Volume to low. Must be > " + limits[chat_data["currency"]]
            update.message.reply_text(msg_error)
            logger.warning(msg_error)

            reply_msg = "Enter new volume"
            cancel_btn = build_menu([KeyboardButton(KeyboardEnum.CANCEL.clean())])
            reply_mrk = ReplyKeyboardMarkup(cancel_btn, resize_keyboard=True)
            update.message.reply_text(reply_msg, reply_markup=reply_mrk)

            return WorkflowEnum.TRADE_VOLUME
    else:
        logger.warning("No minimum order limit in config for coin " + chat_data["currency"])

    trade_show_conf(update, chat_data)

    return WorkflowEnum.TRADE_CONFIRM


# Calculate the volume depending on entered volume type 'VOLUME'
def trade_volume(bot, update, chat_data):
    chat_data["volume"] = "{0:.8f}".format(float(update.message.text.replace(",", ".")))

    # Make sure that the order size is at least the minimum order limit
    if chat_data["currency"] in limits:
        if float(chat_data["volume"]) < float(limits[chat_data["currency"]]):
            msg_error = emo_er + " Volume to low. Must be > " + limits[chat_data["currency"]]
            update.message.reply_text(msg_error)
            logger.warning(msg_error)

            reply_msg = "Enter new volume"
            cancel_btn = build_menu([KeyboardButton(KeyboardEnum.CANCEL.clean())])
            reply_mrk = ReplyKeyboardMarkup(cancel_btn, resize_keyboard=True)
            update.message.reply_text(reply_msg, reply_markup=reply_mrk)

            return WorkflowEnum.TRADE_VOLUME
    else:
        logger.warning("No minimum order limit in config for coin " + chat_data["currency"])

    trade_show_conf(update, chat_data)

    return WorkflowEnum.TRADE_CONFIRM


# Calculate total value and show order description and confirmation for order creation
# This method is used in 'trade_volume' and in 'trade_vol_type_all'
def trade_show_conf(update, chat_data):
    asset_two = assets[chat_data["two"]]["altname"]

    # Generate trade string to show at confirmation
    if chat_data["market_price"]:
        update.message.reply_text(emo_wa + " Retrieving estimated price...")

        # Send request to Kraken to get current trading price for pair
        res_data = kraken.query("Ticker", data={"pair": pairs[chat_data["currency"]]}, private=False)

        # If Kraken replied with an error, show it
        if handle_api_error(res_data, update):
            return

        chat_data["price"] = res_data["result"][pairs[chat_data["currency"]]]["c"][0]

        trade_str = (chat_data["buysell"].lower() + " " +
                     trim_zeros(chat_data["volume"]) + " " +
                     chat_data["currency"] + " @ market price ≈" +
                     trim_zeros(chat_data["price"]) + " " +
                     asset_two)

    else:
        trade_str = (chat_data["buysell"].lower() + " " +
                     trim_zeros(chat_data["volume"]) + " " +
                     chat_data["currency"] + " @ limit " +
                     trim_zeros(chat_data["price"]) + " " +
                     asset_two)

    # If fiat currency, then show 2 digits after decimal place
    if chat_data["two"].startswith("Z"):
        # Calculate total value of order
        total_value = "{0:.2f}".format(float(chat_data["volume"]) * float(chat_data["price"]))
    # Else, show 8 digits after decimal place
    else:
        # Calculate total value of order
        total_value = "{0:.8f}".format(float(chat_data["volume"]) * float(chat_data["price"]))

    if chat_data["market_price"]:
        total_value_str = "(Value: ≈" + str(trim_zeros(total_value)) + " " + asset_two + ")"
    else:
        total_value_str = "(Value: " + str(trim_zeros(total_value)) + " " + asset_two + ")"

    reply_msg = " Place this order?\n" + trade_str + "\n" + total_value_str
    update.message.reply_text(emo_qu + reply_msg, reply_markup=keyboard_confirm())


# The user has to confirm placing the order
def trade_confirm(bot, update, chat_data):
    if update.message.text.upper() == KeyboardEnum.NO.clean():
        return cancel(bot, update, chat_data=chat_data)

    update.message.reply_text(emo_wa + " Placing order...")

    req_data = dict()

    # Order type MARKET
    if chat_data["market_price"]:
        req_data["ordertype"] = "market"
        req_data["trading_agreement"] = "agree"
    # Order type LIMIT
    else:
        req_data["ordertype"] = "limit"
        req_data["price"] = chat_data["price"]

    req_data["type"] = chat_data["buysell"].lower()
    req_data["volume"] = chat_data["volume"]
    req_data["pair"] = pairs[chat_data["currency"]]

    # Send request to create order to Kraken
    res_add_order = kraken.query("AddOrder", req_data, private=True)

    # If Kraken replied with an error, show it
    if handle_api_error(res_add_order, update):
        return

    # If there is a transaction id then the order was placed successfully
    if res_add_order["result"]["txid"]:
        order_txid = res_add_order["result"]["txid"][0]

        req_data = dict()
        req_data["txid"] = order_txid

        # Send request to get info on specific order
        res_query_order = kraken.query("QueryOrders", data=req_data, private=True)

        # If Kraken replied with an error, show it
        if handle_api_error(res_query_order, update):
            return

        if res_query_order["result"][order_txid]:
            order_desc = res_query_order["result"][order_txid]["descr"]["order"]
            msg = emo_fi + " Order placed:\n" + order_txid + "\n" + trim_zeros(order_desc)
            update.message.reply_text(bold(msg), reply_markup=keyboard_cmds(), parse_mode=ParseMode.MARKDOWN)

            # Add Job to JobQueue to check status of created order (if enabled)
            if config["check_trade"]:
                trade_time = config["check_trade_time"]
                context = dict(order_txid=order_txid)
                job_queue.run_repeating(order_state_check, trade_time, context=context)
        else:
            update.message.reply_text("No order with TXID " + order_txid)

    else:
        update.message.reply_text("Undefined state: no error and no TXID")

    clear_chat_data(chat_data)
    return ConversationHandler.END


# Show and manage orders
@restrict_access
def orders_cmd(bot, update):
    update.message.reply_text(emo_wa + " Retrieving orders...")

    # Send request to Kraken to get open orders
    res_data = kraken.query("OpenOrders", private=True)

    # If Kraken replied with an error, show it
    if handle_api_error(res_data, update):
        return

    # Reset global orders list
    global orders
    orders = list()

    # Go through all open orders and show them to the user
    if res_data["result"]["open"]:
        for order_id, order_details in res_data["result"]["open"].items():
            # Add order to global order list so that it can be used later
            # without requesting data from Kraken again
            orders.append({order_id: order_details})

            order_desc = trim_zeros(order_details["descr"]["order"])
            update.message.reply_text(bold(order_id + "\n" + order_desc), parse_mode=ParseMode.MARKDOWN)
    else:
        update.message.reply_text(bold("No open orders"), parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    reply_msg = "What do you want to do?"

    buttons = [
        KeyboardButton(KeyboardEnum.CLOSE_ORDER.clean()),
        KeyboardButton(KeyboardEnum.CLOSE_ALL.clean())
    ]

    close_btn = [
        KeyboardButton(KeyboardEnum.CANCEL.clean())
    ]

    menu = build_menu(buttons, n_cols=2, footer_buttons=close_btn)
    reply_mrk = ReplyKeyboardMarkup(menu, resize_keyboard=True)

    update.message.reply_text(reply_msg, reply_markup=reply_mrk)
    return WorkflowEnum.ORDERS_CLOSE


# Choose what to do with the open orders
def orders_choose_order(bot, update):
    buttons = list()

    # Go through all open orders and create a button
    if orders:
        for order in orders:
            order_id = next(iter(order), None)
            buttons.append(KeyboardButton(order_id))
    else:
        update.message.reply_text("No open orders")
        return ConversationHandler.END

    msg = "Which order to close?"

    close_btn = [
        KeyboardButton(KeyboardEnum.CANCEL.clean())
    ]

    menu = build_menu(buttons, n_cols=1, footer_buttons=close_btn)
    reply_mrk = ReplyKeyboardMarkup(menu, resize_keyboard=True)

    update.message.reply_text(msg, reply_markup=reply_mrk)
    return WorkflowEnum.ORDERS_CLOSE_ORDER


# Close all open orders
def orders_close_all(bot, update):
    update.message.reply_text(emo_wa + " Closing orders...")

    closed_orders = list()

    if orders:
        for x in range(0, len(orders)):
            order_id = next(iter(orders[x]), None)

            # Send request to Kraken to cancel orders
            res_data = kraken.query("CancelOrder", data={"txid": order_id}, private=True)

            # If Kraken replied with an error, show it
            if handle_api_error(res_data, update, "Order not closed:\n" + order_id + "\n"):
                # If we are currently not closing the last order,
                # show message that we a continuing with the next one
                if x+1 != len(orders):
                    update.message.reply_text(emo_wa + " Closing next order...")
            else:
                closed_orders.append(order_id)

        if closed_orders:
            msg = bold(" Orders closed:\n" + "\n".join(closed_orders))
            update.message.reply_text(emo_fi + msg, reply_markup=keyboard_cmds(), parse_mode=ParseMode.MARKDOWN)
        else:
            msg = bold("No orders closed")
            update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
            return
    else:
        msg = bold("No open orders")
        update.message.reply_text(msg, reply_markup=keyboard_cmds(), parse_mode=ParseMode.MARKDOWN)

    return ConversationHandler.END


# Close the specified order
def orders_close_order(bot, update):
    update.message.reply_text(emo_wa + " Closing order...")

    req_data = dict()
    req_data["txid"] = update.message.text

    # Send request to Kraken to cancel order
    res_data = kraken.query("CancelOrder", data=req_data, private=True)

    # If Kraken replied with an error, show it
    if handle_api_error(res_data, update):
        return

    msg = emo_fi + " " + bold("Order closed:\n" + req_data["txid"])
    update.message.reply_text(msg, reply_markup=keyboard_cmds(), parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


# FIXME: Doesn't end the current conversation
# Reloads keyboard with available commands
@restrict_access
def reload_cmd(bot, update):
    msg = emo_wa + " Reloading keyboard..."
    update.message.reply_text(msg, reply_markup=keyboard_cmds())
    return ConversationHandler.END


# Get current state of Kraken API
# Is it under maintenance or functional?
@restrict_access
def state_cmd(bot, update):
    update.message.reply_text(emo_wa + " Retrieving API state...")

    msg = "Kraken API Status: " + bold(kraken.api_state()) + "\nhttps://status.kraken.com"
    updater.bot.send_message(config["user_id"],
                             msg,
                             reply_markup=keyboard_cmds(),
                             disable_web_page_preview=True,
                             parse_mode=ParseMode.MARKDOWN)

    return ConversationHandler.END


def start_cmd(bot, update):
    msg = emo_be + " Welcome to Kraken-Telegram-Bot!"
    update.message.reply_text(msg, reply_markup=keyboard_cmds())


# Shows sub-commands to control the bot
@restrict_access
def bot_cmd(bot, update):
    reply_msg = "What do you want to do?"

    buttons = [
        KeyboardButton(KeyboardEnum.RESTART.clean()),
        KeyboardButton(KeyboardEnum.SHUTDOWN.clean()),
        KeyboardButton(KeyboardEnum.SETTINGS.clean()),
        KeyboardButton(KeyboardEnum.API_STATE.clean()),
        KeyboardButton(KeyboardEnum.CANCEL.clean())
    ]

    reply_mrk = ReplyKeyboardMarkup(build_menu(buttons, n_cols=2), resize_keyboard=True)
    update.message.reply_text(reply_msg, reply_markup=reply_mrk)

    return WorkflowEnum.BOT_SUB_CMD


# Execute chosen sub-cmd of 'bot' cmd
def bot_sub_cmd(bot, update):
    # Restart
    if update.message.text.upper() == KeyboardEnum.RESTART.clean():
        restart_cmd(bot, update)

    # Shutdown
    elif update.message.text.upper() == KeyboardEnum.SHUTDOWN.clean():
        shutdown_cmd(bot, update)

    # API State
    elif update.message.text.upper() == KeyboardEnum.API_STATE.clean():
        state_cmd(bot, update)

    # Cancel
    elif update.message.text.upper() == KeyboardEnum.CANCEL.clean():
        return cancel(bot, update)


# This needs to be run on a new thread because calling 'updater.stop()' inside a
# handler (shutdown_cmd) causes a deadlock because it waits for itself to finish
def shutdown():
    updater.stop()
    updater.is_idle = False


# Terminate this script
@restrict_access
def shutdown_cmd(bot, update):
    update.message.reply_text(emo_go + " Shutting down...", reply_markup=ReplyKeyboardRemove())

    # See comments on the 'shutdown' function
    threading.Thread(target=shutdown).start()


# Restart this python script
@restrict_access
def restart_cmd(bot, update):
    update.message.reply_text(emo_wa + " Bot is restarting...", reply_markup=ReplyKeyboardRemove())

    time.sleep(0.2)
    os.execl(sys.executable, sys.executable, *sys.argv)


# Get current settings
@restrict_access
def settings_cmd(bot, update):
    settings = str()
    buttons = list()

    # Go through all settings in config file
    for key, value in config.items():
        settings += key + " = " + str(value) + "\n\n"
        buttons.append(KeyboardButton(key.upper()))

    # Send message with all current settings (key & value)
    update.message.reply_text(settings)

    cancel_btn = [
        KeyboardButton(KeyboardEnum.CANCEL.clean())
    ]

    msg = "Choose key to change value"

    menu = build_menu(buttons, n_cols=2, footer_buttons=cancel_btn)
    reply_mrk = ReplyKeyboardMarkup(menu, resize_keyboard=True)
    update.message.reply_text(msg, reply_markup=reply_mrk)

    return WorkflowEnum.SETTINGS_CHANGE


# Change setting
def settings_change(bot, update, chat_data):
    chat_data["setting"] = update.message.text.lower()

    # Don't allow to change setting 'user_id'
    if update.message.text.upper() == "USER_ID":
        update.message.reply_text("It's not possible to change USER_ID value")
        return

    msg = "Enter new value"

    update.message.reply_text(msg, reply_markup=ReplyKeyboardRemove())

    return WorkflowEnum.SETTINGS_SAVE


# Save new value for chosen setting
def settings_save(bot, update, chat_data):
    new_value = update.message.text

    # Check if new value is a boolean
    if new_value.lower() == "true":
        chat_data["value"] = True
    elif new_value.lower() == "false":
        chat_data["value"] = False
    else:
        # Check if new value is an integer ...
        try:
            chat_data["value"] = int(new_value)
        # ... if not, save as string
        except ValueError:
            chat_data["value"] = new_value

    msg = " Save new value and restart bot?"
    update.message.reply_text(emo_qu + msg, reply_markup=keyboard_confirm())

    return WorkflowEnum.SETTINGS_CONFIRM


# Confirm saving new setting and restart bot
def settings_confirm(bot, update, chat_data):
    if update.message.text.upper() == KeyboardEnum.NO.clean():
        return cancel(bot, update, chat_data=chat_data)

    # Set new value in config dictionary
    config[chat_data["setting"]] = chat_data["value"]

    # Save changed config as new one
    with open("config.json", "w") as cfg:
        json.dump(config, cfg, indent=4)

    update.message.reply_text(emo_fi + " New value saved")

    # Restart bot to activate new setting
    restart_cmd(bot, update)


# Remove all data from 'chat_data' since we are canceling / ending
# the conversation. If this is not done, next conversation will
# have all the old values
def clear_chat_data(chat_data):
    if chat_data:
        for key in list(chat_data.keys()):
            del chat_data[key]


# Will show a cancel message, end the conversation and show the default keyboard
def cancel(bot, update, chat_data=None):
    # Clear 'chat_data' for next conversation
    clear_chat_data(chat_data)

    # Show the commands keyboard and end the current conversation
    update.message.reply_text(emo_ca + " Canceled...", reply_markup=keyboard_cmds())
    return ConversationHandler.END


# Return chat ID for an update object
def get_chat_id(update=None):
    if update:
        if update.message:
            return update.message.chat_id
        elif update.callback_query:
            return update.callback_query.from_user["id"]
    else:
        return config["user_id"]


# Create a button menu to show in Telegram messages
def build_menu(buttons, n_cols=1, header_buttons=None, footer_buttons=None):
    menu = [buttons[i:i + n_cols] for i in range(0, len(buttons), n_cols)]

    if header_buttons:
        menu.insert(0, header_buttons)
    if footer_buttons:
        menu.append(footer_buttons)

    return menu


# Custom keyboard that shows all available commands
def keyboard_cmds():
    command_buttons = [
        KeyboardButton("/trade"),
        KeyboardButton("/orders"),
        KeyboardButton("/balance"),
        KeyboardButton("/bot")
    ]

    return ReplyKeyboardMarkup(build_menu(command_buttons, n_cols=2), resize_keyboard=True)


# Generic custom keyboard that shows YES and NO
def keyboard_confirm():
    buttons = [
        KeyboardButton(KeyboardEnum.YES.clean()),
        KeyboardButton(KeyboardEnum.NO.clean())
    ]

    return ReplyKeyboardMarkup(build_menu(buttons, n_cols=2), resize_keyboard=True)


# Create a list with a button for every coin in config
def coin_buttons():
    buttons = list()

    for coin in config["used_pairs"]:
        buttons.append(KeyboardButton(coin))

    return buttons


# Check order state and send message if order closed
def order_state_check(bot, job):
    req_data = dict()
    req_data["txid"] = job.context["order_txid"]

    # Send request to get info on specific order
    res_data = kraken.query("QueryOrders", data=req_data, private=True)

    # If Kraken replied with an error, return without notification
    if res_data["error"]:
        error = btfy(res_data["error"][0])
        logger.error(error)
        if config["send_error"]:
            src = "Order state check:\n"
            bot.send_message(chat_id=config["user_id"], text=src + emo_er + " " + error)
        return

    # Save information about order
    order_info = res_data["result"][job.context["order_txid"]]

    # Check if order was canceled. If so, stop monitoring
    if order_info["status"] == "canceled":
        # Stop this job
        job.schedule_removal()
        return

    # Check if trade was executed. If so, stop monitoring and send message
    if order_info["status"] == "closed":
        msg = " Trade executed:\n" + job.context["order_txid"] + "\n" + trim_zeros(order_info["descr"]["order"])
        bot.send_message(chat_id=config["user_id"], text=bold(emo_no + msg), parse_mode=ParseMode.MARKDOWN)
        # Stop this job
        job.schedule_removal()


# Monitor status changes of previously created open orders
def monitor_orders():
    if config["check_trade"]:
        # Send request for open orders to Kraken
        res_data = kraken.query("OpenOrders", private=True)

        # If Kraken replied with an error, show it
        if res_data["error"]:
            error = btfy(res_data["error"][0])
            logger.error(error)
            if config["send_error"]:
                src = "Monitoring orders:\n"
                updater.bot.send_message(chat_id=config["user_id"], text=src + emo_er + " " + error)
            return

        if res_data["result"]["open"]:
            for order in res_data["result"]["open"]:
                # Save order transaction ID
                order_txid = str(order)
                # Save time in seconds from config
                check_trade_time = config["check_trade_time"]

                # Add Job to JobQueue to check status of order
                context = dict(order_txid=order_txid)
                job_queue.run_repeating(order_state_check, check_trade_time, context=context)


# TODO: Complete sanity check
# Check sanity of settings in config file
def is_conf_sane(trade_pairs):
    for setting, value in config.items():
        # Check if user ID is a digit
        if "USER_ID" == setting.upper():
            if not value.isdigit():
                return False, setting.upper()
        # Check if trade pairs are correctly configured,
        # and save pairs in global variable
        elif "USED_PAIRS" == setting.upper():
            global pairs
            for coin, to_cur in value.items():
                found = False
                for pair, data in trade_pairs.items():
                    if coin in pair and to_cur in pair:
                        if not pair.endswith(".d"):
                            pairs[coin] = pair
                            found = True
                if not found:
                    return False, setting.upper() + " - " + coin

    return True, None


def handle_init_error(error, msg, uid, msg_id):
    updater.bot.edit_message_text(emo_fa + msg, chat_id=uid, message_id=msg_id)

    error = btfy(error)
    updater.bot.send_message(uid, emo_er + " " + error)
    logger.error(error)


# Make sure preconditions are met and show welcome screen
def init_cmd(bot, update):
    uid = config["user_id"]
    cmds = "/initialize - retry again\n/shutdown - shut down the bot"

    # Show start up message
    msg = " Preparing Kraken-Bot"
    updater.bot.send_message(uid, emo_be + msg, disable_notification=True, reply_markup=ReplyKeyboardRemove())

    # Assets -----------------

    msg = " Reading assets..."
    m = updater.bot.send_message(uid, emo_wa + msg, disable_notification=True)

    # TODO: encapsulate assets
    success, res_assets = kraken.assets()
    if not success:
        msg = "Reading assets... FAILED\n" + cmds
        return handle_init_error(res_assets, msg, uid, m.message_id)

    global assets
    assets = res_assets

    msg = " Reading assets... DONE"
    updater.bot.edit_message_text(emo_do + msg, chat_id=uid, message_id=m.message_id)

    # Asset pairs -----------------

    msg = " Reading asset pairs..."
    m = updater.bot.send_message(uid, emo_wa + msg, disable_notification=True)

    success, res_pairs = kraken.assets_pairs()
    if not success:
        msg = "Reading asset pairs... FAILED\n" + cmds
        return handle_init_error(res_pairs, msg, uid, m.message_id)

    msg = " Reading asset pairs... DONE"
    updater.bot.edit_message_text(emo_do + msg, chat_id=uid, message_id=m.message_id)

    # Sanity check -----------------

    msg = " Checking sanity..."
    m = updater.bot.send_message(uid, emo_wa + msg, disable_notification=True)

    # Check sanity of configuration file
    # Sanity check not finished successfully
    sane, parameter = is_conf_sane(res_pairs)
    if not sane:
        msg = " Checking sanity... FAILED\n/shutdown - shut down the bot"
        return handle_init_error("Wrong configuration: " + parameter, msg, uid, m.message_id)

    msg = " Checking sanity... DONE"
    updater.bot.edit_message_text(emo_do + msg, chat_id=uid, message_id=m.message_id)

    # Order limits -----------------

    msg = " Reading order limits..."
    m = updater.bot.send_message(uid, emo_wa + msg, disable_notification=True)

    # Save order limits in global variable
    global limits
    limits = kraken.min_order_size()

    msg = " Reading order limits... DONE"
    updater.bot.edit_message_text(emo_do + msg, chat_id=uid, message_id=m.message_id)

    # Bot is ready -----------------

    msg = " Kraken-Bot is ready!"
    updater.bot.send_message(uid, emo_be + msg, reply_markup=keyboard_cmds())


# From pair string (XXBTZEUR) get from-asset (XXBT) and to-asset (ZEUR)
def assets_from_pair(pair):
    for asset, data in assets.items():
        # If TRUE, we know that 'to_asset' exists in assets
        if pair.endswith(asset):
            from_asset = pair[:len(asset)]
            to_asset = pair[len(pair)-len(asset):]

            # If TRUE, we know that 'from_asset' exists in assets
            if from_asset in assets:
                return from_asset, to_asset
            else:
                return None, to_asset

    return None, None


# Returns a pre compiled Regex pattern to ignore case
def comp(pattern):
    return re.compile(pattern, re.IGNORECASE)


# Returns regex representation of OR for all coins in config 'used_pairs'
def regex_coin_or():
    coins_regex_or = str()

    for coin in config["used_pairs"]:
        coins_regex_or += coin + "|"

    return coins_regex_or[:-1]


# Returns regex representation of OR for all fiat currencies in config 'used_pairs'
def regex_asset_or():
    fiat_regex_or = str()

    for asset, data in assets.items():
        fiat_regex_or += data["altname"] + "|"

    return fiat_regex_or[:-1]


# Return regex representation of OR for all settings in config
def regex_settings_or():
    settings_regex_or = str()

    for key, value in config.items():
        settings_regex_or += key.upper() + "|"

    return settings_regex_or[:-1]


def handle_api_error(response, update, additional_msg=""):
    if response["error"]:
        error = btfy(additional_msg + response["error"][0])
        update.message.reply_text(error)
        logger.error(error)
        return True
    return False


def get_api_result(response, update, additional_msg=""):
    if response[0]:
        return response[1]

    error = btfy(additional_msg + response[1])
    update.message.reply_text(error)
    logger.error(error)
    return None


# Handle all telegram and telegram.ext related errors
def handle_telegram_error(bot, update, error):
    error_str = "Update '%s' caused error '%s'" % (update, error)
    logger.error(error_str)

    if config["send_error"]:
        updater.bot.send_message(chat_id=config["user_id"], text=error_str)


# Make sure preconditions are met and show welcome screen
init_cmd(None, None)


# Log all errors
dispatcher.add_error_handler(handle_telegram_error)

# Add command handlers to dispatcher
dispatcher.add_handler(CommandHandler("restart", restart_cmd))
dispatcher.add_handler(CommandHandler("shutdown", shutdown_cmd))
dispatcher.add_handler(CommandHandler("initialize", init_cmd))
dispatcher.add_handler(CommandHandler("balance", balance_cmd))
dispatcher.add_handler(CommandHandler("reload", reload_cmd))
dispatcher.add_handler(CommandHandler("state", state_cmd))
dispatcher.add_handler(CommandHandler("start", start_cmd))


# ORDERS conversation handler
orders_handler = ConversationHandler(
    entry_points=[CommandHandler('orders', orders_cmd)],
    states={
        WorkflowEnum.ORDERS_CLOSE:
            [RegexHandler(comp("^(CLOSE ORDER)$"), orders_choose_order),
             RegexHandler(comp("^(CLOSE ALL)$"), orders_close_all),
             RegexHandler(comp("^(CANCEL)$"), cancel)],
        WorkflowEnum.ORDERS_CLOSE_ORDER:
            [RegexHandler(comp("^(CANCEL)$"), cancel),
             RegexHandler(comp("^[A-Z0-9]{6}-[A-Z0-9]{5}-[A-Z0-9]{6}$"), orders_close_order)]
    },
    fallbacks=[CommandHandler('cancel', cancel)]
)
dispatcher.add_handler(orders_handler)


# TRADE conversation handler
trade_handler = ConversationHandler(
    entry_points=[CommandHandler('trade', trade_cmd)],
    states={
        WorkflowEnum.TRADE_BUY_SELL:
            [RegexHandler(comp("^(BUY|SELL)$"), trade_buy_sell, pass_chat_data=True),
             RegexHandler(comp("^(CANCEL)$"), cancel, pass_chat_data=True)],
        WorkflowEnum.TRADE_CURRENCY:
            [RegexHandler(comp("^(" + regex_coin_or() + ")$"), trade_currency, pass_chat_data=True),
             RegexHandler(comp("^(CANCEL)$"), cancel, pass_chat_data=True),
             RegexHandler(comp("^(ALL)$"), trade_sell_all)],
        WorkflowEnum.TRADE_SELL_ALL_CONFIRM:
            [RegexHandler(comp("^(YES|NO)$"), trade_sell_all_confirm)],
        WorkflowEnum.TRADE_PRICE:
            [RegexHandler(comp("^((?=.*?\d)\d*[.,]?\d*|MARKET PRICE)$"), trade_price, pass_chat_data=True),
             RegexHandler(comp("^(CANCEL)$"), cancel, pass_chat_data=True)],
        WorkflowEnum.TRADE_VOL_TYPE:
            [RegexHandler(comp("^(" + regex_asset_or() + ")$"), trade_vol_asset, pass_chat_data=True),
             RegexHandler(comp("^(VOLUME)$"), trade_vol_volume, pass_chat_data=True),
             RegexHandler(comp("^(ALL)$"), trade_vol_all, pass_chat_data=True),
             RegexHandler(comp("^(CANCEL)$"), cancel, pass_chat_data=True)],
        WorkflowEnum.TRADE_VOLUME:
            [RegexHandler(comp("^^(?=.*?\d)\d*[.,]?\d*$"), trade_volume, pass_chat_data=True),
             RegexHandler(comp("^(CANCEL)$"), cancel, pass_chat_data=True)],
        WorkflowEnum.TRADE_VOLUME_ASSET:
            [RegexHandler(comp("^^(?=.*?\d)\d*[.,]?\d*$"), trade_volume_asset, pass_chat_data=True),
             RegexHandler(comp("^(CANCEL)$"), cancel, pass_chat_data=True)],
        WorkflowEnum.TRADE_CONFIRM:
            [RegexHandler(comp("^(YES|NO)$"), trade_confirm, pass_chat_data=True)]
    },
    fallbacks=[CommandHandler('cancel', cancel, pass_chat_data=True)]
)
dispatcher.add_handler(trade_handler)


# Will return the SETTINGS_CHANGE state for a conversation handler
# This way the state is reusable
def settings_change_state():
    return [WorkflowEnum.SETTINGS_CHANGE,
            [RegexHandler(comp("^(" + regex_settings_or() + ")$"), settings_change, pass_chat_data=True),
             RegexHandler(comp("^(CANCEL)$"), cancel, pass_chat_data=True)]]


# Will return the SETTINGS_SAVE state for a conversation handler
# This way the state is reusable
def settings_save_state():
    return [WorkflowEnum.SETTINGS_SAVE,
            [MessageHandler(Filters.text, settings_save, pass_chat_data=True)]]


# Will return the SETTINGS_CONFIRM state for a conversation handler
# This way the state is reusable
def settings_confirm_state():
    return [WorkflowEnum.SETTINGS_CONFIRM,
            [RegexHandler(comp("^(YES|NO)$"), settings_confirm, pass_chat_data=True)]]


# BOT conversation handler
bot_handler = ConversationHandler(
    entry_points=[CommandHandler('bot', bot_cmd)],
    states={
        WorkflowEnum.BOT_SUB_CMD:
            [RegexHandler(comp("^(RESTART|SHUTDOWN)$"), bot_sub_cmd),
             RegexHandler(comp("^(API STATE)$"), state_cmd),
             RegexHandler(comp("^(SETTINGS)$"), settings_cmd),
             RegexHandler(comp("^(CANCEL)$"), cancel)],
        settings_change_state()[0]: settings_change_state()[1],
        settings_save_state()[0]: settings_save_state()[1],
        settings_confirm_state()[0]: settings_confirm_state()[1]
    },
    fallbacks=[CommandHandler('cancel', cancel)]
)
dispatcher.add_handler(bot_handler)


# SETTINGS conversation handler
settings_handler = ConversationHandler(
    entry_points=[CommandHandler('settings', settings_cmd)],
    states={
        settings_change_state()[0]: settings_change_state()[1],
        settings_save_state()[0]: settings_save_state()[1],
        settings_confirm_state()[0]: settings_confirm_state()[1]
    },
    fallbacks=[CommandHandler('cancel', cancel)]
)
dispatcher.add_handler(settings_handler)


# Write content of configuration file to log
logger.debug("Configuration: " + str(config))

# If webhook is enabled, don't use polling
# https://github.com/python-telegram-bot/python-telegram-bot/wiki/Webhooks
if config["webhook_enabled"]:
    updater.start_webhook(listen=config["webhook_listen"],
                          port=config["webhook_port"],
                          url_path=config["bot_token"],
                          key=config["webhook_key"],
                          cert=config["webhook_cert"],
                          webhook_url=config["webhook_url"])
else:
    # Start polling to handle all user input
    # Dismiss all in the meantime send commands
    updater.start_polling(clean=True)

# Monitor status changes of open orders
monitor_orders()

# Run the bot until you press Ctrl-C or the process receives SIGINT,
# SIGTERM or SIGABRT. This should be used most of the time, since
# start_polling() is non-blocking and will stop the bot gracefully.
updater.idle()
