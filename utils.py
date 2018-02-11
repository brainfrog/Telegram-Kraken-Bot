import datetime

# Emojis for messages
emo_er = "‼"  # Error
emo_wa = "⏳"  # Wait
emo_fi = "🏁"  # Finished
emo_no = "🔔"  # Notify
emo_be = "✨"  # Beginning
emo_ca = "❌"  # Cancel
emo_to = "👍"  # Top
emo_do = "✔"  # Done
emo_fa = "✖"  # Failed
emo_go = "👋"  # Goodbye
emo_qu = "❓"  # Question


# Remove trailing zeros to get clean values
def trim_zeros(value_to_trim):
    if isinstance(value_to_trim, float):
        return ('%.8f' % value_to_trim).rstrip('0').rstrip('.')
    elif isinstance(value_to_trim, str):
        str_list = value_to_trim.split(" ")
        for i in range(len(str_list)):
            old_str = str_list[i]
            if old_str.replace(".", "").isdigit():
                new_str = str(('%.8f' % float(old_str)).rstrip('0').rstrip('.'))
                str_list[i] = new_str
        return " ".join(str_list)
    else:
        return value_to_trim


# Add asterisk as prefix and suffix for a string
# Will make the text bold if used with Markdown
def bold(text):
    return "*" + text + "*"


# Beautifies Kraken error messages
def btfy(text):
    # Remove whitespaces
    text = text.strip()

    new_text = str()

    for x in range(0, len(list(text))):
        new_text += list(text)[x]

        if list(text)[x] == ":":
            new_text += " "

    return emo_er + " " + new_text


# Converts a Unix timestamp to a data-time object with format 'Y-m-d H:M:S'
def datetime_from_timestamp(unix_timestamp):
    return datetime.datetime.fromtimestamp(int(unix_timestamp)).strftime('%Y-%m-%d %H:%M:%S')
