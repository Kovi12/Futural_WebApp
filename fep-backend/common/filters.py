import pandas as pd

def filter_min_metric(df, metric, min_metric):
    return df[df[metric] >= min_metric]

def filter_by_max_metric(df, metric, max_metric):
    return df[df[metric] <= max_metric]

def limit_nr_locations(df, nr_locations, shuffle=True):
    if shuffle:
        return df.sample(n=nr_locations) if len(df) >= nr_locations else df
    return df.head(nr_locations)

def sort_by_metric(df, metric, ascending=True):
    return df.sort_values(by=metric, ascending=ascending)
