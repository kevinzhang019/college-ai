import sys
import os
import openai
from dotenv import load_dotenv
load_dotenv()

def check_openai_api_key(api_key):           # ---> b'sk-proj...' expected

    client = openai.OpenAI(api_key=api_key)
    try:
        client.models.list()
    except openai.AuthenticationError:
        return False
    else:
        return True


raw = os.getenv("OPENAI_API_KEY")
print(repr(raw))

OPENAI_API_KEY = ""

if check_openai_api_key(OPENAI_API_KEY):
    print("Valid OpenAI API key.")
else:
    print("Invalid OpenAI API key.")

from pymilvus import utility
utility.drop_collection("book")