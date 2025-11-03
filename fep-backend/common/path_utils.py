import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Paths for adapters and models
MODEL_ROOT = os.path.join(BASE_DIR, "..")
MODEL_PATHS = {
    "compression": os.path.join(MODEL_ROOT, "Llama3-8b_fn_nometeo"),
    "meteo": os.path.join(MODEL_ROOT, "Llama3-8b_fn"),
    "durangaldea": "unsloth/deepseek-r1-distill-llama-8b-unsloth-bnb-4bit"
}

ADAPTER_PATHS = {
    "compression": "Llama3-8b_fn_nometeo",
    "meteo": "Llama3-8b_fn",
    "durangaldea": "valy3124/durangaldea-assistantFinalPD"
}

# Paths for CSV data
DATA_DIR = os.path.join(MODEL_ROOT, "data")

def get_csv_path(category: str, mode: str):
    return os.path.join(DATA_DIR, f"{category}_{mode}.csv")

def get_street_csv():
    return os.path.join(DATA_DIR, "durangaldea_streets.csv")

def get_worldcities_csv():
    return os.path.join(DATA_DIR, "worldcities.csv")
