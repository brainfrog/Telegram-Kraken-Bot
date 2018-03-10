import krakenex
import inspect
import bs4
import re
import requests
from utils import *
from file_logger import logger


class Kraken(krakenex.API):
    _assets = {}

    def __init__(self, keyfile="kraken.key", retries=0):
        super().__init__()
        self.load_key(keyfile)
        self._retries = retries

    # Issue Kraken API requests
    def query(self, method, data=None, private=False, retries=None):
        # Get arguments of this function
        frame = inspect.currentframe()
        args, _, _, values = inspect.getargvalues(frame)

        # Get name of caller function
        caller = inspect.currentframe().f_back.f_code.co_name

        # Log caller of this function and all arguments
        logger.debug(caller + " - args: " + str([(i, values[i]) for i in args]))

        try:
            if private:
                return self.query_private(method, data)
            else:
                return self.query_public(method, data)

        except Exception as ex:
            logger.exception(self.__class__.__name__ + " exception:")

            ex_name = type(ex).__name__

            # Handle the following exceptions immediately without retrying

            # Mostly this means that the API keys are not correct
            if "Incorrect padding" in str(ex):
                msg = "Incorrect padding: please verify that your Kraken API keys are valid"
                return {"error": [msg]}
            # No need to retry if the API service is not available right now
            elif "Service:Unavailable" in str(ex):
                msg = "Service: Unavailable"
                return {"error": [msg]}

            # Is retrying on error enabled?
            if self._retries:
                # It's the first call, start retrying
                if retries is None:
                    retries = self._retries
                    return self.query(method, data, private, retries)
                # If 'retries' is bigger then 0, decrement it and retry again
                elif retries > 0:
                    retries -= 1
                    return self.query(method, data, private, retries)
                # Return error from last Kraken request
                else:
                    return {"error": [ex_name + ":" + str(ex)]}
            # Retrying on error not enabled, return error from last Kraken request
            else:
                return {"error": [ex_name + ":" + str(ex)]}

    def balance(self):
        # Send request to Kraken to get current balance of all currencies
        res_balance = self.query("Balance", private=True)

        if res_balance["error"]:
            return False, res_balance["error"][0]

        # Send request to Kraken to get open orders
        res_orders = self.query("OpenOrders", private=True)

        if res_orders["error"]:
            return False, res_orders["error"][0]

        msg = str()

        # Go over all currencies in your balance
        for currency_key, currency_value in res_balance["result"].items():
            available_value = currency_value

            # Go through all open orders and check if an order exists for the currency
            if res_orders["result"]["open"]:
                for order in res_orders["result"]["open"]:
                    order_desc = res_orders["result"]["open"][order]["descr"]["order"]
                    order_desc_list = order_desc.split(" ")

                    order_type = order_desc_list[0]
                    order_volume = order_desc_list[1]
                    price_per_coin = order_desc_list[5]

                    # Check if asset is fiat-currency (EUR, USD, ...) and BUY order
                    if currency_key.startswith("Z") and order_type == "buy":
                        available_value = float(available_value) - (float(order_volume) * float(price_per_coin))

                    # Current asset is a coin and not a fiat currency
                    else:
                        for asset, data in self._assets.items():
                            if order_desc_list[2].endswith(data["altname"]):
                                order_currency = order_desc_list[2][:-len(data["altname"])]
                                break

                        # Reduce current volume for coin if open sell-order exists
                        if self._assets[currency_key]["altname"] == order_currency and order_type == "sell":
                            available_value = float(available_value) - float(order_volume)

            # Only show assets with volume > 0
            if trim_zeros(currency_value) is not "0":
                msg += bold(self._assets[currency_key]["altname"] + ": " + trim_zeros(currency_value) + "\n")

                available_value = trim_zeros("{0:.8f}".format(float(available_value)))
                currency_value = trim_zeros("{0:.8f}".format(float(currency_value)))

                # If orders exist for this asset, show available volume too
                if currency_value == available_value:
                    msg += "(Available: all)\n"
                else:
                    msg += "(Available: " + available_value + ")\n"

        return True, msg

    def assets(self):
        res_assets = self.query("Assets")

        if res_assets["error"]:
            return False, res_assets["error"][0]

        self._assets = res_assets["result"]

        return True, self._assets

    def assets_pairs(self):
        res_pairs = self.query("AssetPairs")

        if res_pairs["error"]:
            return False, res_pairs["error"][0]

        return True, res_pairs["result"]

    # Return dictionary with asset name as key and order limit as value
    @staticmethod
    def min_order_size():
        url = "https://support.kraken.com/hc/en-us/articles/205893708-What-is-the-minimum-order-size-"
        response = requests.get(url)

        # If response code is not 200, return empty dictionary
        if response.status_code != 200:
            return {}

        minimum_order_size = dict()

        soup = bs4.BeautifulSoup(response.content, "html.parser")

        for article_body in soup.find_all(class_="article-body"):
            for ul in article_body.find_all("ul"):
                for li in ul.find_all("li"):
                    text = li.get_text().strip()
                    limit = text[text.find(":") + 1:].strip()
                    match = re.search('\((.+?)\)', text)

                    if match:
                        minimum_order_size[match.group(1)] = limit

                return minimum_order_size

    # Return state of Kraken API
    # State will be extracted from Kraken Status website
    @staticmethod
    def api_state():
        url = "https://status.kraken.com"
        response = requests.get(url)

        # If response code is not 200, return state 'UNKNOWN'
        if response.status_code != 200:
            return "UNKNOWN"

        soup = bs4.BeautifulSoup(response.content, "html.parser")

        for comp_inner_cont in soup.find_all(class_="component-inner-container"):
            for name in comp_inner_cont.find_all(class_="name"):
                if "API" in name.get_text():
                    return comp_inner_cont.find(class_="component-status").get_text().strip()
