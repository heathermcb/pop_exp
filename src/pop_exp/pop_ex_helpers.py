import pandas as pd
import geopandas as gpd
import numpy as np
import pyarrow.parquet as pq
import rasterio
import rasterio.windows
from tqdm import tqdm
from rasterio.mask import mask
from shapely.validation import make_valid
from shapely.ops import unary_union
from shapely import wkt
from pathlib import Path
from exactextract import exact_extract
import warnings

warnings.filterwarnings("ignore")
# just here to stop the script complaining about how we're taking centroids
# in a projected instead of geographic crs


# take a lat lon pair and return the best UTM projection for that lat lon
def get_best_utm_projection(lat, lon):
    zone_number = (lon + 180) // 6 + 1
    hemisphere = 326 if lat >= 0 else 327
    epsg_code = hemisphere * 100 + zone_number
    return f"EPSG:{int(epsg_code)}"


# add UTM projection column to a geodataframe initially only containing
# climate hazard IDs and a geometry column. this column will contain the best
# UTM projection for the centroid of each geometry
# -----------------------------------------------------------------------------
# will call this before running any buffering in final functions,
# as buffering is done in meters and we want to make sure we're using the
# best UTM projection for the data to minimize distortion.
# note that data is in WGS84
# this is a geographic crs so should get correct lat/lon
# centroid calculation may be slightly off if the data is in a projected crs
# but we're trading that off to get correct lat/lon
# also note centroid for points is just the point itself
def add_utm_projection(ch_shp: gpd.GeoDataFrame):

    # get lat and lon
    ch_shp["centroid_lon"] = ch_shp.centroid.x
    ch_shp["centroid_lat"] = ch_shp.centroid.y

    # get projection for each hazard
    ch_shp["utm_projection"] = ch_shp.apply(
        lambda row: get_best_utm_projection(
            lat=row["centroid_lat"], lon=row["centroid_lon"]
        ),
        axis=1,
    )

    # select id, geometry, buffer dist, and utm projection
    ch_shp = ch_shp[["ID_climate_hazard", "buffer_dist", "geometry", "utm_projection"]]

    return ch_shp


# read in a geo data file from either parquet or geojson
def load_ch(path_to_hazard: str) -> gpd.GeoDataFrame:
    """Function to load data"""
    path = Path(path_to_hazard)

    # Check the file extension
    if path.suffix == ".geojson":
        data = gpd.read_file(path)
    elif path.suffix == ".parquet":
        data = gpd.read_parquet(path)
    else:
        raise FileNotFoundError(f"File not found or unsupported file type: {path}")

    return data


# read in a climate hazard shapefile or spatial unit shapefile (counties,
# zcta, etc) in parquet format that contains a string column with the geom ID
# and a geography column, but nothing else. this function renames the ID cols
# consistently, makes geoms valid, adds a column indicating the best UTM
# projection, and reprojects to WGS84
# hazard ID should be a string and geometry should be the geographies column and
# must be named "geometry"
# --------------------------------------------------------------------------
# need some checks and to throw errors if the dataframe cols are not the
# correct type or if there are more than two cols
# - more than 2
# - wrong types
# - no geometry col
# include these in the validations, and have those be part of a validation
# function that runs first
def prep_geographies(shp_path: str, geo_type: str):
    # print message
    if geo_type == "hazard":
        print(
            f"Reading data and finding best UTM projection for hazard geometries (1/4)"
        )
    elif geo_type == "spatial_unit":
        print(f"Reading spatial unit geometries (1/4)")

    # read in data
    shp_df = load_ch(shp_path)

    # remove missing geoms
    shp_df = shp_df[~shp_df["geometry"].is_empty]

    # make valid geoms, esp important for hazards
    shp_df["geometry"] = shp_df["geometry"].apply(make_valid)

    # reproject to WGS84
    if shp_df.crs != "EPSG:4326":
        shp_df = shp_df.to_crs("EPSG:4326")

    # if hazard, add best projection
    if geo_type == "hazard":
        shp_df = add_utm_projection(shp_df)

    return shp_df


# mutate a dataframe containing climate hazards: buffer each climate hazard
# geometry, based on previously ided distances, and add new col containing a
# new buffered hazard geometry
def add_buffered_geom_col(ch_df: gpd.GeoDataFrame):
    for index, row in tqdm(
        ch_df.iterrows(), total=len(ch_df), desc="Buffering hazard geometries (2/4)"
    ):
        best_utm = row["utm_projection"]
        hazard_geom = row["geometry"]

        # create geoseries in best projection
        geom_series = gpd.GeoSeries([hazard_geom], crs=ch_df.crs)
        geom_series_utm = geom_series.to_crs(best_utm)

        # buffer distance is in meters
        buffer_dist = row["buffer_dist"]
        buffered_hazard_geometry = geom_series_utm.buffer(buffer_dist).iloc[0]
        # back to OG
        buffered_hazard_geometry = (
            gpd.GeoSeries([buffered_hazard_geometry], crs=best_utm)
            .to_crs(ch_df.crs)
            .iloc[0]
        )
        # add
        ch_df.at[index, "buffered_hazard"] = buffered_hazard_geometry

    return ch_df


# prep data: this function takes in path names to climate hazards and additional
# geographies, and calls the above helpers to read in data, find best UTM crs
# for each climate hazard, add buffer distances, and buffer hte hazards.
# it returns a geodataframe with the hazard IDs, the original hazard geometry,
# best UTM projection, buffer distance, and buffered hazard geometry. if there
# are additional geos, it returns a tuple of the above plus a dataframe
# containing the additional geo IDs and geometries.
def prep_data(
    path_to_hazards: str,
    path_to_additional_geos: str = None,
):

    # prep both geographies
    ch_shp = prep_geographies(path_to_hazards, geo_type="hazard")
    # if additional_geos isn't None, do this step too
    if path_to_additional_geos:
        ad_geo = prep_geographies(path_to_additional_geos, geo_type="spatial_unit")

    # add buffered hazard geometry col to climate hazards
    ch_shp = add_buffered_geom_col(ch_shp)

    if path_to_additional_geos:
        return ch_shp, ad_geo
    else:
        return ch_shp


# take a geodataframe of geometries (named 'geometry' column) and their IDs,
# and if two geometries overlap, combine them via unary union into one geometry.
# if more than two overlap, combine them all via unary union into one geometry.
# the IDs of any geometries that are combined are concatonated with underscores
# in between. but if geometries do not overlap they are untouched and IDs are
# untouched. return a dataframe of new unioned geometries and their IDs
# IDs will have the same name as the input ID column, geom called 'geometry'
# ------------------------------------------------------------------------
# this function is hacky. if anyone has ideas to improve please!
# also there is some complexity. in the test dataset sometimes there is one
# fire that is two geometries combined. in that case, this function first splits
# those up into two geometries both with the same ID, and then finds geometries
# that overlap with those invididually.
# ------------------------------------------------------------------------
# this means that the result of 'find people affected' will be a list of
# combined geometries (fires that overlapped in the dataset) and those affected.
# if you want to find people affected by
# a specific fire, you might need to sum over all the geometries that are part
# of that fire. for geos, for each zcta (or similar) you'll get people affected
# by fires that overlapped and overlapped the zcta. there might be more than
# one group of fires/hazards
def combine_overlapping_geometries(ch_df: gpd.GeoDataFrame, id_column: str):
    # step 1: explode the data
    # step 2: unary union the data
    # step 3: explode again
    # step 4: rejoin names

    # expand data frame to climate hazards that are not overlapping
    ch_df = ch_df.explode()

    # get unary union of all geometries in the dataset
    all_one_geometry = unary_union(ch_df["geometry"])

    # get non-overlapping hazards by exploding all_one_geometry
    non_overlapping_hazards = gpd.GeoDataFrame(
        {"geometry": [all_one_geometry]}, crs=ch_df.crs
    ).explode()

    # rejoin IDs
    # join the IDs back to the non-overlapping hazards
    joined = gpd.sjoin(
        non_overlapping_hazards, ch_df, how="left", predicate="intersects"
    )

    # drop geometry to group by that column
    joined["geometry_wkt"] = joined["geometry"].apply(lambda geom: geom.wkt)
    without_geom = joined.drop(columns="geometry")

    # group by geometry_wkt, and aggregate by concatenating the
    # ID_climate_hazard into one string
    without_geom = (
        without_geom.groupby("geometry_wkt")
        .agg({id_column: lambda x: "___".join(x)})
        .reset_index()
    )
    # add back geoms
    combined_geoms = gpd.GeoDataFrame(
        without_geom,
        geometry=without_geom["geometry_wkt"].apply(wkt.loads),
        crs=ch_df.crs,
    )
    # select id and geometry and name right
    combined_geoms = combined_geoms[[id_column, "geometry"]]

    return combined_geoms


# mutate a dataframe containing climate hazards: create envelop buffer for
# the geodataframe's geometries, and add these as a col to the ch dataframe.
# doing this to make it easy to load raster for pop only in the bounding box
# of the hazard for ~*swiftness*~. arg to add a buffer to the bounding box if
# going to use to calculate pop dens
def add_bounding_box_col(
    ch_df: gpd.GeoDataFrame, target_col: str, buffer_buffer: int = 0
):
    # if we're not doing pop dens the bounding box should be no larger than
    # the buffered geography. for pop dens need room for radius of convolution
    # kernel
    for index, row in tqdm(
        ch_df.iterrows(), total=len(ch_df), desc="Adding bounding box (3/4)"
    ):
        target_col_geom = row[target_col]
        bounding_box = target_col_geom.envelope.buffer(buffer_buffer)
        ch_df.at[index, "bounding_box"] = bounding_box

    return ch_df


# this function does the fast raster read in piece of the larger functions
# it takes in a dataframe with buffered hazard geometries and bounding boxes,
# and it opens and masks a raster to find how many people are in those
# geographies.
# def mask_raster_with_geoms(ch_df: gpd.GeoDataFrame, raster_path: str):
#     # find num of people in geoms
#     for index, row in tqdm(
#         ch_df.iterrows(), total=len(ch_df), desc="Masking raster (4/4)"
#     ):
#         bounding_box = row["bounding_box"]
#         buffered_hazard_geometry = row["geometry"]

#         # load raster data only in bounding box, need bounding box to mol
#         mol_crs = "ESRI:54009"
#         bbox_transformed = (
#             gpd.GeoSeries([bounding_box], crs="EPSG:4326").to_crs(mol_crs).iloc[0]
#         )

#         # open the raster file
#         with rasterio.open(raster_path) as src:
#             # read raster data within the bounding box
#             window = src.window(*bbox_transformed.bounds)
#             pop_data = src.read(1, window=window)
#             pop_transform = src.window_transform(window)

#             # mask w buffered hazard so we can sum over hazard area
#             buffered_hazard_geometry = gpd.GeoSeries(
#                 [buffered_hazard_geometry], crs="EPSG:4326"
#             ).to_crs(src.crs)
#             out_image, out_transform = mask(
#                 src,
#                 buffered_hazard_geometry.geometry,
#                 crop=True,
#                 filled=True,
#                 nodata=0,
#                 all_touched=True,
#             )

#             # sum over hazard area and save
#             pop_sum = np.sum(out_image)
#             ch_df.at[index, "num_people_affected"] = pop_sum

#     return ch_df


# mask raster partial pixel
def mask_raster_partial_pixel(ch_df: gpd.GeoDataFrame, raster_path: str):
    print("Masking raster: 4/4")
    # Open the raster file
    with rasterio.open(raster_path) as src:
        # Ensure CRS alignment
        if ch_df.crs != src.crs:
            ch_df = ch_df.to_crs(src.crs)

    # Use exact_extract to calculate population sums for each geometry
    # it returns a dictionary so we need to get out the right stuff
    num_people_affected = exact_extract(
        raster_path,
        ch_df,
        "sum",
    )
    sums = [hazard["properties"]["sum"] for hazard in num_people_affected]
    ch_df["num_people_affected"] = sums
    # project ch_df back to wgs84
    ch_df = ch_df.to_crs("EPSG:4326")

    # final df
    return ch_df


# take a climate hazard dataframe with buffered hazard geoms and bounding boxes,
# and a gridded pop dataset, and return the number of people within the buffer
# each climate hazard/the number of people affected by each hazard
# ---------------------------------------------------------
# note: this is doing binary masking, and including pixels if they touch the
# geom. can improve this if we want to calc the proportion of pixel covered by
# the hazard
def find_num_people_affected(
    path_to_hazards: str, raster_path: str, by_unique_hazard: bool
):
    print(f"Running find_num_people_affected")
    # prep data
    # get ID, hazard geom, best UTM, buffer dist, and buffered geom in WGS84
    ch_df = prep_data(path_to_hazards=path_to_hazards)

    # find overlapping buffered hazards
    # select and rename columns in filtered_ch - ID, and set buffered hazard to be geom
    ch_df = ch_df[["ID_climate_hazard", "buffered_hazard"]]
    # rename buffered hazard to geometry
    ch_df = ch_df.rename(columns={"buffered_hazard": "geometry"})
    ch_df = ch_df.set_geometry("geometry", crs="EPSG:4326")

    if not by_unique_hazard:
        ch_df = combine_overlapping_geometries(ch_df, id_column="ID_climate_hazard")

    # add bounding box col
    ch_df = add_bounding_box_col(ch_df, target_col="geometry")

    # find num of people affected
    ch_df = mask_raster_partial_pixel(ch_df, raster_path)

    # select ID and num of people affected
    ch_df = ch_df[["ID_climate_hazard", "num_people_affected"]]

    # final df
    return ch_df


# take path to climate hazard shapefile, path to additional
# geographies, such as ZCTAs or counties, and a raster dataset of gridded
# population data, and return a dataframe with the number of people affected by
# each climate hazard in each geography
# ---------------------------------------------------------
# read in a climate hazard shapefile or spatial unit shapefile (counties,
# zcta, etc) in parquet format that contains a string column with the geom ID
# and a geography column, but nothing else.
# def find_num_people_affected_by_geo(
#     path_to_hazards: str,
#     path_to_additional_geos: str,
#     raster_path: str,
#     by_unique_hazard: bool,
# ):
#     # prep data
#     # get ID, hazard geom, best UTM, buffer dist, and buffered geom in WGS84
#     # also get ad geos in WGS84
#     ch_shp, ad_geo = prep_data(
#         path_to_hazards=path_to_hazards, path_to_additional_geos=path_to_additional_geos
#     )

#     # find overlapping buffered hazards
#     # select and rename columns in filtered_ch - ID, and set buffered hazard to be geom
#     ch_shp = ch_shp[["ID_climate_hazard", "buffered_hazard"]]
#     # rename buffered hazard to geometry
#     ch_shp = ch_shp.rename(columns={"buffered_hazard": "geometry"})
#     ch_shp = ch_shp.set_geometry("geometry")
#     # call
#     if not by_unique_hazard:
#         ch_shp = combine_overlapping_geometries(ch_shp, id_column="ID_climate_hazard")

#     # intersect buffered hazards w spatial units
#     # intersection gives new dataframe with hazard ID, unit ID, and piece of geo
#     # intersecting w each buffered hazard
#     # set active geom to buffered hazard
#     ch_shp = ch_shp.set_geometry("geometry", crs="EPSG:4326")
#     unit_hazard_intersection = gpd.overlay(ch_shp, ad_geo, how="intersection")

#     # get bounding boxes for unit hazard pieces combined
#     unit_hazard_intersection = add_bounding_box_col(
#         unit_hazard_intersection, target_col="geometry"
#     )

#     # find num of people affected by each piece
#     num_af = mask_raster_with_geoms(unit_hazard_intersection, raster_path)

#     # select columns
#     num_af = num_af[["ID_climate_hazard", "ID_spatial_unit", "num_people_affected"]]

#     return num_af


# option with partial pixel
def find_num_people_affected_by_geo(
    path_to_hazards: str,
    path_to_additional_geos: str,
    raster_path: str,
    by_unique_hazard: bool,
):
    # prep data
    # get ID, hazard geom, best UTM, buffer dist, and buffered geom in WGS84
    # also get ad geos in WGS84
    ch_shp, ad_geo = prep_data(
        path_to_hazards=path_to_hazards, path_to_additional_geos=path_to_additional_geos
    )

    # find overlapping buffered hazards
    # select and rename columns in filtered_ch - ID, and set buffered hazard to be geom
    ch_shp = ch_shp[["ID_climate_hazard", "buffered_hazard"]]
    # rename buffered hazard to geometry
    ch_shp = ch_shp.rename(columns={"buffered_hazard": "geometry"})
    ch_shp = ch_shp.set_geometry("geometry")
    # call
    if not by_unique_hazard:
        ch_shp = combine_overlapping_geometries(ch_shp, id_column="ID_climate_hazard")

    # intersect buffered hazards w spatial units
    # intersection gives new dataframe with hazard ID, unit ID, and piece of geo
    # intersecting w each buffered hazard
    # set active geom to buffered hazard
    ch_shp = ch_shp.set_geometry("geometry", crs="EPSG:4326")
    unit_hazard_intersection = gpd.overlay(ch_shp, ad_geo, how="intersection")

    # get bounding boxes for unit hazard pieces combined
    unit_hazard_intersection = add_bounding_box_col(
        unit_hazard_intersection, target_col="geometry"
    )

    # find num of people affected by each piece
    num_af = mask_raster_partial_pixel(unit_hazard_intersection, raster_path)

    # select columns
    num_af = num_af[["ID_climate_hazard", "ID_spatial_unit", "num_people_affected"]]

    return num_af


# find number of people by geography
# finds number of people residing in each additional geography
def find_number_of_people_residing_by_geo(
    path_to_additional_geos: str, raster_path: str
):

    # prep geographies
    ad_geo = prep_geographies(path_to_additional_geos, geo_type="spatial_unit")

    # add bounding box col
    ad_geo = add_bounding_box_col(ad_geo, target_col="geometry")

    # mask raster and find people by geo
    num_people_by_geo = mask_raster_partial_pixel(ad_geo, raster_path)

    num_people_by_geo = num_people_by_geo[["ID_spatial_unit", "num_people_affected"]]

    return num_people_by_geo
