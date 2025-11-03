import csv
from geopy.geocoders import Nominatim
from math import radians, cos, sin, sqrt, atan2
import pandas as pd
import common.filters as flt
import common.utils as ut
import requests
import os
from common.path_utils import get_street_csv

from geopy.exc import GeocoderTimedOut
from difflib import get_close_matches


# ############## PENTRU DISTANTE
# # Funcția Haversine pentru calculul distanței dintre două coordonate
# def haversine(lat1, lon1, lat2, lon2):
#     R = 6371000  # Raza Pământului în metri
#     lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
#     dlat = lat2 - lat1
#     dlon = lon2 - lon1

#     a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
#     c = 2 * atan2(sqrt(a), sqrt(1 - a))
#     return R * c

# # Găsește cea mai apropiată locație
# def get_closest_location(df, latitude, longitude):
#     if df is None or df.empty:
#         return None

#     df["haversine_distance"] = df.apply(
#         lambda row: haversine(latitude, longitude, row["latitude"], row["longitude"]),
#         axis=1
#     )

#     # Găsim rândul cu cea mai mică distanță
#     closest_row = df.loc[df["haversine_distance"].idxmin()]
#     return closest_row["distance"], closest_row["time"], closest_row["latitude"], closest_row["longitude"]


# ##############CITIREA DATELOR DIN FISIER
# def get_file_path(category, mode):
#     """
#     Determines the correct CSV file path based on the category and mode.
#     :param category: str, One of ["hospitals", "pharmacies", "supermarkets"]
#     :param mode: str, One of ["bike", "drive", "walk"]
#     :return: str, The file path for the selected category and mode
#     """
#     valid_categories = ["hospitals", "pharmacies", "supermarkets"]
#     valid_modes = ["bike", "drive", "walk"]

#     if category not in valid_categories or mode not in valid_modes:
#         raise ValueError(f"Invalid category '{category}' or mode '{mode}'. Choose from {valid_categories} and {valid_modes}.")

#     return f"{category}_{mode}.csv"  # Example: hospitals_bike.csv

# # Functie pentru citirea fisierului CSV intr-un DataFrame
# def load_data(category, mode):
#     file_path = get_file_path(category, mode)
#     try:
#         df = pd.read_csv(file_path)
#         df.columns = ["distance", "time", "latitude", "longitude"]  # Asigurare columne
#         df = df.dropna().astype({"distance": float, "time": float, "latitude": float, "longitude": float})
#         return df
#     except FileNotFoundError:
#         print(f"Error: The file {file_path} does not exist.")
#         return None



########### FUNCTIA PRINCIPALA
def get_closest_distance_time(category, mode, location=None, metric_to_extract=None, min_metric=None, max_metric=None, nr_locations=1, sort_min=False, sort_max=False):
    """
    Găsește cea mai apropiată locație pe baza coordonatelor utilizatorului.
    
    :param category: str, Tipul de locație ("hospitals", "pharmacies", "supermarkets")
    :param mode: str, Modul de transport ("bike", "drive", "walk")
    :DEFAULT param latitude: float, Latitudine - In caz ca este o strada, oras, regiune mentionata
    :DEFAULT param longitude: float, Longitudine - In caz ca este o strada, oras, regiune mentionata
    :DEFAULT param metric_to_extract: String, Filtrare dupa ce vreau sa filtrez - Ex: In cate MINUTE ajung... ; Care este distanta pana la...; values - distance, time
    :DEFAULT param min_metric: float, Filtrare pentru metrica minimă (opțional) - Ex: se afla la minimum x metri~minute
    :DEFAULT param max_metric: float, Filtrare pentru metrica maxima (opțional) - Ex: se afla la maximum x metri~minute
    :DEFAULT nr_locations: number, Numarul de locatii pe care il va intoarce - Ex: "Ofera-mi [X] locatii"
    :DEFAULT sort_min: Boolean, Filtrare care sorteaza crescator DF - Ex: cele mai APROPIATE...
    :DEFAULT sort_max: Boolean, Filtrare care sorteaza descrescator DF - ex: cele mai DEPARTATE...
    :return: tuple (distanță, timp, latitudine, longitudine) sau None dacă nu sunt rezultate
    """
    
    #"Suport pentru intrebarea: Ofera-mi 5 locatii in care ajung sub 5 min pana la un spital. -> nr_locations=5, metric_to_extract=time, max_metric=5, 
    "Ofera-mi 3 locatii in care ajung sub 2km pana la un spital."

    ""
    print("\n===== FUNCTION CALL DEBUG =====")
    print(f"Function called with parameters:")
    print(f"  category: {category} (type: {type(category)})")
    print(f"  mode: {mode} (type: {type(mode)})")
    print(f"  location: {location} (type: {type(location)})")
    print(f"  metric_to_extract: {metric_to_extract} (type: {type(metric_to_extract)})")
    print(f"  min_metric: {min_metric} (type: {type(min_metric)})")
    print(f"  max_metric: {max_metric} (type: {type(max_metric)})")
    print(f"  nr_locations: {nr_locations} (type: {type(nr_locations)})")
    print(f"  sort_min: {sort_min} (type: {type(sort_min)})")
    print(f"  sort_max: {sort_max} (type: {type(sort_max)})")
    print("================================\n")
    # Încarcă datele inițiale
    df = ut.load_data(category, mode)
    
    if df.empty:
        print("Nu există date încărcate.")
        return None
    
    coordinates = None
    if location:
        # Attempt to get coordinates from the CSV file
        try:
            df_streets = pd.read_csv(get_street_csv())
            city, street = location.split(", ")
            matching_row = df_streets[(df_streets['City'] == city) & (df_streets['Street Name'] == street)]
            if not matching_row.empty:
                coordinates = (matching_row['Latitude'].values[0], matching_row['Longitude'].values[0])
            else:
                coordinates = ut.get_coordinates(location)  # Fallback to get_coordinates if not found
        except FileNotFoundError:
            print("Error: The file durangaldea_streets.csv does not exist.")
            coordinates = ut.get_coordinates(location)  # Fallback if CSV file is missing

    if coordinates:
        df = ut.get_closest_location(df,coordinates[0], coordinates[1], nr_locations)
        
    else:
        # Aplicare filtre folosind funcțiile din filters.py
        print(f"\n---- Debug: Starting filtering process ----")
        print(f"Initial DataFrame shape: {df.shape}")
        print(f"metric_to_extract: {metric_to_extract}, min_metric: {min_metric}, max_metric: {max_metric}")
        print(f"sort_min: {sort_min}, sort_max: {sort_max}, nr_locations: {nr_locations}")
        
        if metric_to_extract in ["distance", "time"]:
            print(f"Using metric: {metric_to_extract}")
            
            if min_metric is not None:
                print(f"Applying min_metric filter: {min_metric}")
                df = flt.filter_min_metric(df, metric_to_extract, min_metric)
                print(f"After min_metric filter, DataFrame shape: {df.shape}")
            
            if max_metric is not None:
                print(f"Applying max_metric filter: {max_metric}")
                df = flt.filter_by_max_metric(df, metric_to_extract, max_metric)
                print(f"After max_metric filter, DataFrame shape: {df.shape}")
            
            if sort_min:
                print(f"Sorting by {metric_to_extract} in ascending order")
                df = flt.sort_by_metric(df, metric_to_extract, ascending=True)
                print(f"DataFrame sorted, first few values: {df[metric_to_extract].head().tolist()}")
            
            if sort_max:
                print(f"Sorting by {metric_to_extract} in descending order")
                df = flt.sort_by_metric(df, metric_to_extract, ascending=False)
                print(f"DataFrame sorted, first few values: {df[metric_to_extract].head().tolist()}")

        # Limitează numărul locațiilor finale returnate
        print(f"Limiting to {nr_locations} locations")
        if sort_max or sort_min:
            print("Using ordered limitation (no shuffle)")
            df = flt.limit_nr_locations(df, nr_locations, shuffle=False)
        else:
            print("Using random limitation (with shuffle)")
            df = flt.limit_nr_locations(df, nr_locations)
        
        print(f"After limitation, DataFrame shape: {df.shape}")

        # Verificare după filtrare
        if df.empty:
            print("No locations satisfy the conditions - returning None")
            return None
        
        # Get addresses and coordinates for each location
        print("Getting addresses and coordinates for each location")
        addresses = []
        coordinates = []
        for idx, row in df.iterrows():
            print(f"Processing row {idx}: lat={row['latitude']}, lon={row['longitude']}")
            address = ut.get_address_from_coordinates(row['latitude'], row['longitude'])
            if address:
                addr_str = f"{address[0]}, {address[1]}"
                print(f"Got address: {addr_str}")
                addresses.append(addr_str)
            else:
                print("No address found, using 'Unknown address'")
                addresses.append("Unknown address")
            
            # Append coordinates
            coordinates.append({
                "latitude": row['latitude'],
                "longitude": row['longitude']
            })

        print(f"Total addresses found: {len(addresses)}")
        print(f"Addresses: {addresses}")
        print(f"Coordinates: {coordinates}")
        print("Returning addresses and coordinates")
        return {"addresses": addresses, "coordinates": coordinates}

    
    # Return based on metric_to_extract
    if metric_to_extract == "distance":
        return [{"distance": row["distance"]} for _, row in df.iterrows()]
    elif metric_to_extract == "time":
        return [{"time": row["time"]} for _, row in df.iterrows()]
    else:
        return [
            {
                "distance": row["distance"],
                "time": row["time"],
                "latitude": row["latitude"],
                "longitude": row["longitude"]
            }
            for _, row in df.iterrows()
        ]










def get_coordinates(city_name):
    """
    Gets the latitude and longitude of a given city name.
    :param city_name: str, Name of the city
    :return: tuple (latitude, longitude) or None if not found
    """
    geolocator = Nominatim(user_agent="geo_locator")
    location = geolocator.geocode(city_name)

    if location:
        return location.latitude, location.longitude
    else:
        return None





def print_min_max_from_csvs():
    categories = ["hospitals", "pharmacies", "supermarkets"]
    modes = ["bike", "drive", "walk"]

    for category in categories:
        for mode in modes:
            file_path = f"{category}_{mode}.csv"  # Construct the file name
            if os.path.exists(file_path):  # Check if the file exists
                try:
                    df = pd.read_csv(file_path)
                    # Adjusting column names to match the CSV structure
                    df.columns = ["Distance in metres", "Time in minutes", "latitude", "longitude"]
                    
                    # Filter out zero values
                    df = df[(df['Distance in metres'] > 0) & (df['Time in minutes'] > 0)]

                    # Calculate min and max for distance and time
                    min_distance = df['Distance in metres'].min() if not df.empty else None
                    max_distance = df['Distance in metres'].max() if not df.empty else None
                    min_time = df['Time in minutes'].min() if not df.empty else None
                    max_time = df['Time in minutes'].max() if not df.empty else None

                    print(f"Category: {category}, Mode: {mode}")
                    print(f"  Min Distance: {min_distance} m, Max Distance: {max_distance} m")
                    print(f"  Min Time: {min_time} min, Max Time: {max_time} min")
                except Exception as e:
                    print(f"Error reading {file_path}: {str(e)}")
            else:
                print(f"File {file_path} does not exist.")

# Call the function to execute
if __name__ == "__main__":
    #print_min_max_from_csvs()


    category = "pharmacies"
    mode = "walk"
    latitude, longitude = 43.7908347, -2.6542222

    result = get_closest_distance_time(category="supermarkets", mode="drive", metric_to_extract="distance", max_metric=4320.0, nr_locations=3)

    if result is not None:
        print(result)

    # known_locations = [
    # "Hospital de Gernika",
    # "Farmacia Urizar",
    # "Solokoa kalea",
    # "Avenida de Gipuzkoa hiribidea",
    # "Supermercado BM Durango",
    # "Farmacia Arana",
    # "Ermua Health Center"]
    # for location in known_locations:
    #     coords = get_coordinates_fuzzy(location)
    #     print(coords)

    # lat = 43.1681
    # lon = -2.6311

    # street, city = ut.get_address_from_coordinates(43.15412970529713,-2.5523942387204017)

    # print(f"Nearest street: {street}")
    # print(f"City: {city}")


    # Get coordinates of a city
    city = "Avenida de Gipuzkoa hiribidea"
    coordinates = get_coordinates(city)
    if coordinates:
        print(f"Coordinates of {city}: Latitude {coordinates[0]}, Longitude {coordinates[1]}")
    else:
        print("City not found.")
        

