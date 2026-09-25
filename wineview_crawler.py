import time
import html
import re
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import gspread
from gspread_dataframe import set_with_dataframe


# ============================================================
# Configuration
# ============================================================

BASE_API_URL = (
    "https://wineview.com.hk/wp-json/wc/store/products"
    "?per_page=100&orderby=date&order=desc"
)

CREDENTIALS_FILE = (
    Path(__file__).resolve().parent / "credentials.json"
)

SPREADSHEET_KEY = "1PA3CIUYLy7p_Qr4InNmWVEtcJItAh48cOIO3_keJ26o"
WORKSHEET_NAME = "All Products"
TARGET_WORKSHEET_ROWS = 9999

MAX_RETRIES = 5
SLEEP_BETWEEN = 3
BACKOFF_BASE = 3
TIMEOUT = 30


# ============================================================
# API functions
# ============================================================

def create_session():
    """
    Create a requests session with automatic retry logic for
    temporary HTTP errors.
    """
    session = requests.Session()

    retries = Retry(
        total=MAX_RETRIES,
        connect=MAX_RETRIES,
        read=MAX_RETRIES,
        status=MAX_RETRIES,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False
    )

    adapter = HTTPAdapter(max_retries=retries)

    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update({
        "User-Agent": "Mozilla/5.0"
    })

    return session


def fetch_page(session, url):
    """
    Fetch one API page.

    Returns:
        list: Parsed JSON response.
        None: If all retry attempts fail.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(url, timeout=TIMEOUT)

            if response.status_code == 200:
                return response.json()

            wait_seconds = BACKOFF_BASE * attempt

            print(
                f"Status {response.status_code} on attempt "
                f"{attempt}/{MAX_RETRIES}. "
                f"Retrying in {wait_seconds} seconds..."
            )

            time.sleep(wait_seconds)

        except (
            requests.exceptions.SSLError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError
        ) as error:

            wait_seconds = BACKOFF_BASE * attempt

            print(
                f"Attempt {attempt}/{MAX_RETRIES} failed: "
                f"{type(error).__name__}. "
                f"Retrying in {wait_seconds} seconds..."
            )

            time.sleep(wait_seconds)

        except requests.exceptions.RequestException as error:
            wait_seconds = BACKOFF_BASE * attempt

            print(
                f"Request error on attempt {attempt}/{MAX_RETRIES}: "
                f"{error}. Retrying in {wait_seconds} seconds..."
            )

            time.sleep(wait_seconds)

        except ValueError as error:
            wait_seconds = BACKOFF_BASE * attempt

            print(
                f"JSON decode error on attempt "
                f"{attempt}/{MAX_RETRIES}: {error}. "
                f"Retrying in {wait_seconds} seconds..."
            )

            time.sleep(wait_seconds)

    print(
        f"Giving up after {MAX_RETRIES} attempts: {url}"
    )

    return None


def fetch_all_products(base_url):
    """
    Fetch all product pages and return the raw product data
    as a pandas DataFrame.
    """
    all_products = []
    page = 1

    with create_session() as session:

        while True:
            url = f"{base_url}&page={page}"

            print(f"Fetching page {page}...")

            data = fetch_page(session, url)

            if data is None:
                raise RuntimeError(
                    f"Could not fetch page {page} after "
                    f"{MAX_RETRIES} attempts."
                )

            if not isinstance(data, list):
                raise TypeError(
                    f"Unexpected API response on page {page}. "
                    f"Expected list, received {type(data).__name__}."
                )

            if not data:
                print("Reached the end of the product pages.")
                break

            for item in data:
                product = parse_product(item, page)
                all_products.append(product)

            print(
                f"Page {page} completed. "
                f"Total products fetched: {len(all_products):,}"
            )

            page += 1
            time.sleep(SLEEP_BETWEEN)

    columns_order = [
        "ID",
        "Name",
        "Type",
        "Country",
        "Region",
        "Sub Region",
        "Producer",
        "Grape",
        "Format",
        "Inventory Qty",
        "Price",
        "Sale Price",
        "On Sale",
        "Discount %",
        "Description",
        "Link",
        "Page"
    ]

    return pd.DataFrame(
        all_products,
        columns=columns_order
    )


# ============================================================
# Product parsing functions
# ============================================================

def clean_html(raw_html):
    """
    Convert HTML content into plain text.
    """
    if not raw_html:
        return ""

    soup = BeautifulSoup(str(raw_html), "html.parser")

    return soup.get_text(
        separator=" ",
        strip=True
    )


def extract_attribute(attributes, attribute_name):
    """
    Extract all terms for a specific product attribute.
    """
    if not isinstance(attributes, list):
        return None

    for attribute in attributes:
        current_name = str(
            attribute.get("name", "")
        ).strip()

        if current_name.casefold() == attribute_name.casefold():
            terms = attribute.get("terms", [])

            values = [
                str(term.get("name", "")).strip()
                for term in terms
                if term.get("name")
            ]

            return ", ".join(values) if values else None

    return None


def convert_price(price_raw):
    """
    Convert WooCommerce minor-unit price into a rounded
    whole-number price.
    """
    if price_raw in (None, ""):
        return None

    try:
        return round(int(price_raw) / 100)

    except (TypeError, ValueError):
        return None


def calculate_discount(price_raw, sale_price_raw):
    """
    Calculate the discount percentage using raw price values.
    """
    try:
        regular_price = int(price_raw)
        sale_price = int(sale_price_raw)

    except (TypeError, ValueError):
        return None

    if regular_price <= 0:
        return None

    if regular_price == sale_price:
        return None

    return round(
        (regular_price - sale_price) / regular_price * 100
    )


def parse_categories(categories):
    """
    Convert the category list into a comma-separated string.
    """
    if not isinstance(categories, list):
        return None

    category_names = [
        str(category.get("name", "")).strip()
        for category in categories
        if category.get("name")
    ]

    return ", ".join(category_names) if category_names else None


def parse_product(item, page):
    """
    Parse one API product record into the required output structure.
    """
    categories = item.get("categories", [])
    attributes = item.get("attributes", [])
    prices = item.get("prices") or {}

    price_raw = prices.get("regular_price")
    sale_price_raw = prices.get("sale_price")

    add_to_cart = item.get("add_to_cart") or {}

    return {
        "ID": item.get("id"),
        "Name": item.get("name"),
        "Type": parse_categories(categories),
        "Country": extract_attribute(attributes, "Country"),
        "Region": extract_attribute(attributes, "Region"),
        "Sub Region": extract_attribute(attributes, "Sub Region"),
        "Producer": extract_attribute(attributes, "Producer"),
        "Grape": extract_attribute(attributes, "Grapes"),
        "Format": item.get("weight"),
        "Inventory Qty": add_to_cart.get("maximum"),
        "Price": convert_price(price_raw),
        "Sale Price": convert_price(sale_price_raw),
        "On Sale": item.get("on_sale"),
        "Discount %": calculate_discount(
            price_raw,
            sale_price_raw
        ),
        "Description": clean_html(
            item.get("short_description")
        ),
        "Link": item.get("permalink"),
        "Page": page
    }


# ============================================================
# Data processing functions
# ============================================================

def extract_year(text):
    """
    Extract a four-digit vintage year.

    NV, N.V., MV and M.V. are standardized to NV.
    """
    if not isinstance(text, str):
        return ""

    year_match = re.search(
        r"\b(19\d{2}|20\d{2})\b",
        text
    )

    if year_match:
        return year_match.group(1)

    non_vintage_match = re.search(
        r"(?<![A-Za-z])(?:N\.?\s*V\.?|M\.?\s*V\.?)(?![A-Za-z])",
        text,
        flags=re.IGNORECASE
    )

    if non_vintage_match:
        return "NV"

    return ""


def decode_html_entities(value):
    """
    Decode HTML entities for string values.
    """
    if isinstance(value, str):
        return html.unescape(value)

    return value


def standardize_product_type(type_series):
    """
    Standardize product category names.
    """
    mapping = {
        "White Wine": "White",
        "Red Wine": "Red",
        "Nature Wine": "Nature",
        "Sparkling Wine": "Sparkling",
        "Dessert Wine": "Dessert",
        "Fortified Wine": "Fortified"
    }

    result = type_series.copy()

    for old_value, new_value in mapping.items():
        result = result.str.replace(
            old_value,
            new_value,
            regex=False
        )

    return result


def process_products(df):
    """
    Clean and transform the raw product DataFrame.

    Processing includes:
    1. Removing records with blank or null product types.
    2. Decoding HTML entities.
    3. Extracting vintage.
    4. Standardizing product types.
    5. Formatting the On Sale field.
    6. Removing internal-use columns.
    7. Removing duplicate product records.
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError("df must be a pandas DataFrame.")

    required_columns = {
        "ID",
        "Name",
        "Type",
        "On Sale",
        "Page"
    }

    missing_columns = required_columns.difference(df.columns)

    if missing_columns:
        raise ValueError(
            "Missing required columns: "
            + ", ".join(sorted(missing_columns))
        )

    processed_df = df.copy()

    # Decode HTML entities across the DataFrame
    processed_df = processed_df.map(
        decode_html_entities
    )

    # Normalize blank strings before filtering
    processed_df["Type"] = (
        processed_df["Type"]
        .astype("string")
        .str.strip()
        .replace("", pd.NA)
    )

    # Remove rows with null or blank product types
    processed_df = processed_df.dropna(
        subset=["Type"]
    ).copy()

    # Remove duplicate products using product ID
    processed_df = processed_df.drop_duplicates(
        subset=["ID"],
        keep="first"
    )

    # Format the sale indicator
    processed_df["On Sale"] = (
        processed_df["On Sale"]
        .map({
            True: "Yes",
            False: None
        })
    )

    # Extract vintage from the product name
    vintage = processed_df["Name"].apply(
        extract_year
    )

    name_position = processed_df.columns.get_loc("Name")

    processed_df.insert(
        loc=name_position + 7,
        column="Vintage",
        value=vintage
    )

    # Standardize product types
    processed_df["Type"] = standardize_product_type(
        processed_df["Type"]
    )

    # Remove columns not required in the spreadsheet
    processed_df = processed_df.drop(
        columns=["ID", "Page"],
        errors="ignore"
    )

    # Reset the index before exporting
    processed_df = processed_df.reset_index(
        drop=True
    )

    return processed_df


# ============================================================
# Google Sheets functions
# ============================================================

def update_google_sheet(
    df,
    credentials_file,
    spreadsheet_key,
    worksheet_name,
    target_rows=9999
):
    """
    Replace the worksheet contents with the DataFrame and resize
    the worksheet to exactly the specified number of rows.
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError("df must be a pandas DataFrame.")

    required_rows = len(df) + 1

    if required_rows > target_rows:
        raise ValueError(
            f"The DataFrame requires {required_rows:,} worksheet "
            f"rows including the header, but target_rows is "
            f"{target_rows:,}."
        )

    print(
        f"Connecting to Google Sheets worksheet: "
        f"{worksheet_name}"
    )

    google_client = gspread.service_account(
        filename=credentials_file
    )

    spreadsheet = google_client.open_by_key(
        spreadsheet_key
    )

    worksheet = spreadsheet.worksheet(
        worksheet_name
    )

    # Clear existing values and formatting-independent cell contents
    worksheet.clear()

    # Write the DataFrame
    set_with_dataframe(
        worksheet,
        df,
        include_index=False,
        include_column_header=True,
        resize=False
    )

    # Set the worksheet to exactly the required row count
    worksheet.resize(
        rows=target_rows
    )

    print(
        f"Uploaded {len(df):,} products. "
        f"Worksheet resized to exactly {target_rows:,} rows."
    )


# ============================================================
# Main execution function
# ============================================================

def main():
    """
    Run the complete product extraction, transformation and
    Google Sheets upload process.
    """
    print("Starting product refresh...")

    raw_products = fetch_all_products(
        BASE_API_URL
    )

    print(
        f"Raw products fetched: {len(raw_products):,}"
    )

    df_products = process_products(
        raw_products
    )

    print(
        f"Products after processing: {len(df_products):,}"
    )

    update_google_sheet(
        df=df_products,
        credentials_file=CREDENTIALS_FILE,
        spreadsheet_key=SPREADSHEET_KEY,
        worksheet_name=WORKSHEET_NAME,
        target_rows=TARGET_WORKSHEET_ROWS
    )

    print("Product refresh completed successfully.")


if __name__ == "__main__":
    main()