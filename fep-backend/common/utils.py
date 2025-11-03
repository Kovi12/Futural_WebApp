import pandas as pd
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut
from math import radians, cos, sin, sqrt, atan2
from geopy.distance import distance


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))

def get_file_path(category, mode):
    """
    Determines the correct CSV file path based on the category and mode.
    :param category: str, One of ["hospitals", "pharmacies", "supermarkets"]
    :param mode: str, One of ["bike", "drive", "walk"]
    :return: str, The file path for the selected category and mode
    """
    valid_categories = ["hospitals", "pharmacies", "supermarkets"]
    valid_modes = ["bike", "drive", "walk"]

    if category not in valid_categories or mode not in valid_modes:
        raise ValueError(f"Invalid category '{category}' or mode '{mode}'. Choose from {valid_categories} and {valid_modes}.")

    return f"{category}_{mode}.csv"  # Example: hospitals_bike.csv

# Functie pentru citirea fisierului CSV intr-un DataFrame
def load_data(category, mode):
    file_path = get_file_path(category, mode)
    try:
        df = pd.read_csv(file_path)
        df.columns = ["distance", "time", "latitude", "longitude"]  # Asigurare columne
        df = df.dropna().astype({"distance": float, "time": float, "latitude": float, "longitude": float})
        return df
    except FileNotFoundError:
        print(f"Error: The file {file_path} does not exist.")
        return None


def get_closest_location(df, latitude, longitude, nr_locations=1):
    df["current_distance"] = df.apply(
        lambda row: haversine(latitude, longitude, row["latitude"], row["longitude"]), axis=1
    )
    df_sorted = df.sort_values(by="current_distance")
    return df_sorted.head(nr_locations)


def haversine(lat1, lon1, lat2, lon2):
    from math import radians, cos, sin, sqrt, atan2
    R = 6371000
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
    c = 2*atan2(sqrt(a), sqrt(1-a))
    return R * c

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

def get_address_from_coordinates(latitude, longitude, max_radius=500, step=50):
    """
    Searches incrementally in a radius until a named street is found or max_radius is reached.
    
    :param latitude: float
    :param longitude: float
    :param max_radius: int (maximum search radius in meters)
    :param step: int (step radius increment in meters)
    :return: tuple (street_name, city_name) or None if not found
    """
    geolocator = Nominatim(user_agent="geo_locator")

    def reverse_search(lat, lon):
        try:
            location = geolocator.reverse((lat, lon), language="en", zoom=18)
            if location and location.raw and "address" in location.raw:
                address = location.raw["address"]
                street = address.get("road") or address.get("pedestrian") or address.get("footway")
                city = address.get("town") or address.get("village") or address.get("hamlet") or address.get("municipality")
                if street and city:
                    return street, city
            return None
        except GeocoderTimedOut:
            return None

    # First try original coordinates
    result = reverse_search(latitude, longitude)
    if result:
        return result

    # If original coordinates don't yield a result, incrementally search in circles around the point
    directions = [(0, 1), (1, 0), (0, -1), (-1, 0),
                  (1, 1), (-1, -1), (1, -1), (-1, 1)]

    for radius in range(step, max_radius + step, step):
        for dx, dy in directions:
            new_location = distance(meters=radius).destination((latitude, longitude), bearing=dx * 90 + dy * 45)
            new_lat, new_lon = new_location.latitude, new_location.longitude
            result = reverse_search(new_lat, new_lon)
            if result:
                return result

    return None

    

