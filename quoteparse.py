import re
import json
from dotenv import load_dotenv
import os
from pymongo.mongo_client import MongoClient

# I need to add a filter of the stuff that has ":" because its a link (https://)
# Dashes should really only be taken after quotation marks (otherwise stuff like tear-jerking is counted)
# Need to do something about mike the hacker

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

with open("quotes.json", "r", encoding="utf-8") as f:
   data = json.load(f)

load_dotenv()
MONGO_URI = os.getenv('uri')
mclient = MongoClient(MONGO_URI)
db = mclient["quote-game"]
quote_collection = db.quotes

for item in data:
    result = parse_quote(item)
    if result:
        quote_collection.insert_one(result)
