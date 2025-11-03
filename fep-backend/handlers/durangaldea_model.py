import re
from unsloth import FastLanguageModel
import torch
from transformers import AutoTokenizer
from .script_locatie import get_closest_distance_time

MODEL_PATH = "unsloth/deepseek-r1-distill-llama-8b-unsloth-bnb-4bit"
ADAPTER_PATH = "valy3124/durangaldea-assistantFinalPD"

tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH)
model, _ = FastLanguageModel.from_pretrained(
    model_name=MODEL_PATH,
    max_seq_length=1024,
    dtype=torch.float16,
    load_in_4bit=True,
    device_map="auto",
    use_safetensors=True
)
model.load_adapter(ADAPTER_PATH)
FastLanguageModel.for_inference(model)

def extract_api_call_from_answer(response_text):
    parts = response_text.split("### Answer:", 1)
    answer_text = parts[1] if len(parts) == 2 else response_text
    match = re.search(r"<API>(.*?)</API>", answer_text, re.DOTALL)
    return match.group(1).strip() if match else None

def parse_api_call(call_str):
    pattern = r'(\w+)\s*=\s*(?:"([^"]+)"|([\d.]+))'
    matches = re.findall(pattern, call_str)
    kwargs = {}
    for match in matches:
        key, str_value, num_value = match
        kwargs[key] = str_value if str_value else float(num_value) if '.' in num_value else int(num_value)
    return kwargs

def extract_answer_only(response_text):
    parts = response_text.split("### Answer:", 1)
    return parts[1].strip() if len(parts) == 2 else response_text.strip()

def run_durangaldea_model(question: str) -> str:
    from datetime import datetime

    intent_prompt = f"""Classify the intent of the following question as either "location_travel" or "other":

    {question}

    Intent:"""

    with open("f_webapp.out", "a") as log:
        log.write(f"[DEBUG] Before intent generation\n")
    inputs_intent = tokenizer([intent_prompt], return_tensors="pt").to("cuda")
    intent_ids = model.generate(**inputs_intent, max_new_tokens=10, do_sample=False)
    with open("f_webapp.out", "a") as log:
        log.write(f"[DEBUG] After intent generation\n")

    intent_result = tokenizer.decode(intent_ids[0], skip_special_tokens=True).strip().lower()
    with open("f_webapp.out", "a") as log:
        log.write(f"[DEBUG] After intent decoding\n")

    with open("f_webapp.out", "a") as log:
        log.write(f"[INTENT RAW OUTPUT] {repr(intent_result)}\n")
        log.write(f"[INTENT FINAL PARSED] {intent_result}\n")

    if ("location_travel" not in intent_result) and ("other" not in intent_result):
        with open("f_webapp.out", "a") as log:
            log.write(f"[INTENT ERROR] Unexpected intent result: {repr(intent_result)}\n")
        return "This question is not related to locations or travel in Durangaldea."
    with open("f_webapp.out", "a") as log:
        log.write(f"\n[INTENT RAW OUTPUT] {repr(intent_result)}\n")
    with open("f_webapp.out", "a") as log:
        log.write(f"\n[{datetime.now()}] DURANGALDEA INTENT: {intent_result}\n")

    if "location_travel" not in intent_result:
        fallback = "This question is not related to locations or travel in Durangaldea. I'm here to help with questions about locations, distances, and travel. Please rephrase your question accordingly!"
        with open("f_webapp.out", "a") as log:
            log.write(f"[INTENT FAIL] Query: {question}\nOutput: {fallback}\n")
        return fallback

    prompt = f"""Below is a question about locations in Durangaldea. Your task is to provide a clear and accurate answer using the <API> call whenever necessary. There are two types of questions you may encounter, each with a specific API call format.

    Type 1: Closest Location
    Use this API format to find the closest place of a given category from a specific location:

    <API>get_closest_distance_time(category, mode, location, metric_to_extract) -> result</API>

    Parameters:
    - category: One of ["hospitals", "pharmacies", "supermarkets"]
    - mode: Transportation mode, one of ["drive", "walk", "bike"]
    - location: City and street name in Durangaldea (e.g., "Durango, Artekalea Kalea")
    - metric_to_extract: "distance" or "time"

    The result is returned as a JSON object with:
    {{ "distance": <float km>, "time": <float minutes> }}

    Type 2: Filtered Location List
    Use this API format when the question asks for [X] locations within [Y] km or minutes:
    Example:
    "Give me 3 locations where I can reach a supermarket in under 2 km using walk."
    <API>get_closest_distance_time(category, mode, metric_to_extract, max_metric, nr_locations) -> result</API>

    Parameters:
    - category: One of ["hospitals", "pharmacies", "supermarkets"]
    - mode: One of ["drive", "walk", "bike"]
    - metric_to_extract: "distance" or "time"
    - max_metric: Maximum allowed metric (e.g., 2000 for 2 km)
    - nr_locations: Number of locations to return (e.g., 3)

    Result JSON:
    {{
      "addresses": [
        "Street 1, City",
        "Street 2, City",
        …
      ]
    }}

    Instructions:
    Before answering, identify the question type and build a logical response. Use plain language to explain API call results.

    ### Question:
    {question}

    ### Answer:
    """

    inputs = tokenizer([prompt], return_tensors="pt").to("cuda")
    output_ids = model.generate(**inputs, max_new_tokens=512)
    response = tokenizer.decode(output_ids[0], skip_special_tokens=True)
    with open("f_webapp.out", "a") as log:
        log.write(f"[GENERATION RAW]\n{response}\n")

    api_call = extract_api_call_from_answer(response)
    with open("f_webapp.out", "a") as log:
        log.write(f"[API CALL EXTRACTED] {repr(api_call)}\n")
    api_result = None

    if api_call:
        try:
            kwargs = parse_api_call(api_call.split(")", 1)[0] + ")")
            with open("f_webapp.out", "a") as log:
                log.write("\n" + "="*50)
                log.write("DEBUG - API CALL KWARGS:")
                for key, value in kwargs.items():
                    log.write(f"{key}: {value} ({type(value)})")
            api_result = get_closest_distance_time(**kwargs)
            if not isinstance(api_result, list):
                api_result = [api_result] if api_result else []
        except Exception as e:
            api_result = [{"error": str(e)}]

    cleaned = re.sub(r"(?s)(.*?)<API>.*", r"\1", extract_answer_only(response)).strip()
    with open("f_webapp.out", "a") as log:
        log.write(f"[CLEANED OUTPUT] {repr(cleaned)}\n")

    if api_result and isinstance(api_result, list) and len(api_result) > 0:
        first_result = api_result[0]
        if isinstance(first_result, dict):
            if "distance" in first_result:
                distance_km = float(first_result["distance"]) / 1000
                return f"{cleaned} {distance_km:.2f}km away"
            elif "time" in first_result:
                time_value = float(first_result["time"])
                return f"{cleaned} {time_value:.1f} minutes away"
            elif "addresses" in first_result:
                return f"{cleaned} {'; '.join(first_result['addresses'])}"
    elif cleaned:
        with open("f_webapp.out", "a") as log:
            log.write(f"[DEBUG] No API result, returning cleaned output. {cleaned}\n")
        return cleaned

    return extract_answer_only(response).strip() or "No relevant information found."
