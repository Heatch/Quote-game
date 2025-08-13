import re

def parse_quote(quote_data):
    content = quote_data["content"]
    quoter = quote_data["author"]["display_name"]

    names = []
    processed = content

    # Replace and collect dialogue style names: Name: text
    def repl_dialogue(m):
        name = m.group(1).strip()
        actual = quoter if name.lower() == "me" else name.lower()
        names.append(actual)
        return f"###:{m.group(2)}"
    processed = re.sub(r'(?m)^([^\s:]+):\s*(.*)', repl_dialogue, processed)

    # Replace and collect hyphen style names: "quote" - name OR unquoted - name
    # This regex finds all instances of -name (with optional spaces)
    def repl_hyphen(m):
        name = m.group(1).strip()
        actual = quoter if name.lower() == "me" else name.lower()
        names.append(actual)
        return " - ###"
    processed = re.sub(r'-\s*([^\s,]+)', repl_hyphen, processed)

    if not names:
        return None

    return {
        "quoter": quoter,
        "quote": processed,
        "name": names
    }
