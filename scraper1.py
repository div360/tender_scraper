from dotenv import load_dotenv
load_dotenv()
import os
import requests
from bs4 import BeautifulSoup
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import re
import pymongo
import certifi
import logging
import time

# Configure logging: you can adjust the level (DEBUG, INFO, etc.) as needed.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- MongoDB Setup ---
MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    logger.error("MONGO_URI not set in environment")
    raise Exception("Please set the MONGO_URI environment variable.")

try:
    client = pymongo.MongoClient(MONGO_URI, tls=True, tlsCAFile=certifi.where())
    logger.info("Connected to MongoDB. Databases: %s", client.list_database_names())
except Exception as e:
    logger.error("Failed to connect to MongoDB: %s", e)
    raise e

db = client["tender_db"]
tender_collection = db["tenders"]

logger.info("EMAIL_FROM: %s", os.environ.get("EMAIL_FROM"))

# --- Constants ---
BASE_URL = "https://eproc.rajasthan.gov.in"
MAIN_URL = BASE_URL + "/nicgep/app?page=FrontEndTendersByOrganisation&service=page"
headers = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/15.0 Safari/605.1.15"
    )
}

# Create a persistent session object.
session = requests.Session()

# List of department names to search.
departments_to_search = os.environ.get("DEPARTMENTS").split(",")
# Optionally, strip extra whitespace:
departments_to_search = [dept.strip() for dept in departments_to_search]

def fetch_page(url):
    """
    Use a persistent session to fetch the page.
    If the response indicates a timed-out session, restart the session.
    """
    logger.info("Fetching URL: %s", url)
    try:
        response = session.get(url, headers=headers)
        response.raise_for_status()
        if "Your session has timed out" in response.text:
            logger.warning("Session timed out. Restarting session for URL: %s", url)
            restart_url = BASE_URL + "/nicgep/app?service=restart"
            session.get(restart_url, headers=headers)
            response = session.get(url, headers=headers)
            response.raise_for_status()
        logger.info("Successfully fetched URL: %s", url)
        return response.text
    except requests.RequestException as e:
        logger.error("Error fetching %s: %s", url, e)
        return None


def save_failed_html(html, tender_url):
    """
    Saves the failed HTML content to a text file with a unique name.
    The file is stored in a 'failed_tender_html' directory.
    """
    # Ensure the directory exists
    directory = "failed_tender_html"
    if not os.path.exists(directory):
        os.makedirs(directory)
    
    # Create a safe filename using a sanitized version of the tender URL and a timestamp
    safe_url = re.sub(r'[^a-zA-Z0-9]', '_', tender_url)
    filename = os.path.join(directory, f"failed_{safe_url}_{int(time.time())}.txt")
    
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html)
    
    logger.info("Saved failed tender HTML to %s", filename)

def get_department_table(html):
    """
    Get the department table from the main page.
    (Here we assume it is the third table with class "list_table" – adjust if needed.)
    """
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table", class_="list_table")
    if not tables or len(tables) < 3:
        logger.error("Desired department table not found!")
        return None
    logger.info("Found department table.")
    return tables[2]

def extract_department_link(department_table, dept_name):
    """
    In the department table, search each row for a department whose
    organisation name exactly matches dept_name. Return the full URL.
    """
    for row in department_table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue
        org_name = cells[1].get_text(strip=True)
        if org_name == dept_name:
            a_tag = cells[2].find("a")
            if a_tag and a_tag.has_attr("href"):
                relative_link = a_tag["href"].strip()
                full_link = BASE_URL + relative_link
                logger.info("Found department '%s' link: %s", dept_name, full_link)
                return full_link
    logger.warning("Department '%s' not found in table.", dept_name)
    return None

def get_tender_links_from_org_page(org_html):
    """
    From the organisation (tender list) page, assume the tender table is the first table
    with class "list_table" and extract all tender links from the column containing
    "Title and Ref.No./Tender ID" (assumed to be the 5th column).
    """
    soup = BeautifulSoup(org_html, "html.parser")
    table = soup.find_all("table", class_="list_table")[0]
    tender_links = []
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 5:
            continue
        a_tag = cells[4].find("a")
        if a_tag and a_tag.has_attr("href"):
            relative_link = a_tag["href"].strip()
            full_link = BASE_URL + relative_link
            tender_links.append(full_link)
    logger.info("Extracted %d tender links from organisation page.", len(tender_links))
    return tender_links

def get_tender_dates(soup):
    """
    Extracts tender dates from the BeautifulSoup parsed HTML.
    Returns a dictionary with the date information.
    """
    def extract_date(label):
        b_tag = soup.find("b", string=lambda text: text and label in text)
        if b_tag:
            parent_td = b_tag.find_parent("td")
            if parent_td:
                next_td = parent_td.find_next_sibling("td")
                if next_td:
                    return next_td.get_text(strip=True)
        return None

    tender_dates = {
        "published_date": extract_date("Published Date"),
        "sale_start_date": extract_date("Document Download / Sale Start Date"),
        "clarification_start_date": extract_date("Clarification Start Date"),
        "bid_submission_start_date": extract_date("Bid Submission Start Date"),
        "bid_opening_date": extract_date("Bid Opening Date"),
        "sale_end_date": extract_date("Sale End Date"),
        "clarification_end_date": extract_date("Clarification End Date"),
        "bid_submission_end_date": extract_date("Bid Submission End Date")
    }
    logger.debug("Tender dates extracted: %s", tender_dates)
    return tender_dates

def extract_value(soup, label):
    """
    Finds a <td> with class 'td_caption' whose text (even if nested) contains the given label,
    then returns the text from its immediate sibling <td>.
    """
    td = soup.find("td", class_="td_caption", string=lambda text: text and re.search(label, text, re.IGNORECASE))
    if td:
        sibling = td.find_next_sibling("td")
        if sibling:
            value = sibling.get_text(strip=True)
            logger.debug("Extracted value for label '%s': %s", label, value)
            return value
    logger.warning("Could not extract value for label: %s", label)
    return None

def get_tender_id_organization_chain(soup):
    """
    Extracts the tender id and organization chain from the tender detail page.
    Assumes the first row contains the organization chain and the third row contains the tender id.
    """
    table = soup.find("table", class_="tablebg")
    if not table:
        logger.error("Could not find table with class 'tablebg'.")
        return None, None

    rows = table.find_all("tr")
    if len(rows) < 3:
        logger.error("Not enough rows in the table to extract tender id and organization chain.")
        return None, None

    first_row = rows[0]
    third_row = rows[2]

    tender_id_tds = third_row.find_all("td")
    organisation_chain_tds = first_row.find_all("td")

    try:
        tender_id = tender_id_tds[1].find("b").get_text(strip=True)
        tender_organization_chain = organisation_chain_tds[1].find("b").get_text(strip=True)
        logger.info("Extracted Tender ID: %s", tender_id)
        logger.info("Extracted Organisation Chain: %s", tender_organization_chain)
    except Exception as e:
        logger.error("Error extracting tender id or organization chain: %s", e)
        return None, None

    return tender_id, tender_organization_chain

def get_tender_value(detail_html):
    """
    Extracts tender details from the tender detail page.
    Only returns data for tenders with a value less than 3000000.
    """
    soup = BeautifulSoup(detail_html, "html.parser")
    tender_dates = get_tender_dates(soup)
    
    label_td = soup.find("td", string=lambda text: text and "Tender Value in ₹" in text)
    if label_td:
        value_td = label_td.find_next_sibling("td")
        if value_td:
            value_text = value_td.get_text(strip=True)
            clean_text = value_text.replace(",", "").replace("₹", "").strip()
            try:
                # If the cleaned text is "NA" (or empty), set tender_value to None
                if clean_text.upper() == "NA" or clean_text == "":
                    tender_value = None
                    logger.info("Tender value not available (NA).")
                else:
                    tender_value = int(float(clean_text))
                    logger.info("Tender value extracted: %d", tender_value)

                if tender_value is None or tender_value < 3000000:
                    tender_id, tender_organization_chain = get_tender_id_organization_chain(soup)
                    tender_type = extract_value(soup, "Tender Type")
                    
                    return {
                        "tender_id": tender_id,
                        "tender_value": tender_value,
                        "tender_organization_chain": tender_organization_chain,
                        "tender_type": tender_type,
                        "tender_dates": tender_dates
                    }
                else:
                    logger.warning("Tender value >= 3000000")
                    return {
                        "tender_id": "SKIP"
                    }
            except ValueError:
                logger.error("Could not convert tender value: %s", clean_text)
                return None
    logger.warning("Tender value not found or tender_value >= 3000000")
    return None

def send_email(body):
    sender_email = os.environ.get("EMAIL_FROM")
    recipient_email = os.environ.get("EMAIL_TO")
    smtp_server = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_username = os.environ.get("SMTP_USER")
    smtp_password = os.environ.get("SMTP_PASSWORD")

    logger.info("Sending email from %s to %s...", sender_email, recipient_email)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Tender List"
    msg["From"] = sender_email
    msg["To"] = recipient_email
    msg.attach(MIMEText(body, "html"))

    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_username, smtp_password)
        server.sendmail(sender_email, recipient_email, msg.as_string())
        server.quit()
        logger.info("Email sent successfully.")
    except Exception as e:
        logger.error("Error sending email: %s", e)

if __name__ == "__main__":
    logger.info("Starting tender scraper...")
    email_body = "<html><body>"
    main_page_html = fetch_page(MAIN_URL)
    if not main_page_html:
        logger.error("Main page could not be fetched. Exiting.")
        exit(1)
    
    department_table = get_department_table(main_page_html)
    if not department_table:
        logger.error("Department table not found. Exiting.")
        exit(1)
    
    # Process each department in the list.
    for dept in departments_to_search:
        count=0
        logger.info("Processing department: %s", dept)
        dept_link = extract_department_link(department_table, dept)
        email_body += f"<h2>Department: {dept}</h2>"
        if not dept_link:
            email_body += "<p>Not found or no link available.</p>"
            continue
        
        org_page_html = fetch_page(dept_link)
        if not org_page_html:
            email_body += "<p>Failed to fetch organisation page.</p>"
            continue
        
        tender_links = get_tender_links_from_org_page(org_page_html)
        email_body += f"<p>Found {len(tender_links)} total tenders.</p>"
        
        for tender_url in tender_links:
            logger.info("Processing tender URL: %s", tender_url)
            detail_html = fetch_page(tender_url)
            if not detail_html:
                logger.warning("Failed to fetch tender details for URL: %s", tender_url)
                continue
            tender_values = get_tender_value(detail_html)
            if tender_values is None:
                save_failed_html(detail_html, tender_url)
                email_body += f"<p>Failed to fetch tender details for <a href='{tender_url}'>{tender_url}</a></p>"
                continue
            
            if tender_values["tender_id"] == "SKIP":
                logger.info("Tender value >= 3000000. Skipping.")
                continue

            tender_id = tender_values["tender_id"]
            if not tender_id:
                logger.warning("Tender ID not extracted for URL: %s", tender_url)
                continue

            # Check if this tender has been processed already.
            if tender_collection.find_one({"tender_id": tender_id}):
                logger.info("Tender ID %s already processed. Skipping.", tender_id)
                continue
            
            count+=1
            tender_collection.insert_one({"tender_id": tender_id})
            logger.info("Tender ID %s inserted into DB.", tender_id)
            
            email_body += (
                f"<p><a href='{tender_url}'>Tender URL</a><br>"
                f"Tender ID: {tender_values['tender_id']}<br>"
                f"Tender Value in ₹: {tender_values['tender_value']}<br>"
                f"Tender Type: {tender_values['tender_type']}<br>"
                f"Organization Chain: {tender_values['tender_organization_chain']}<br>"
                f"<b>Critical Dates:</b><br>"
            )

            tender_dates = tender_values["tender_dates"]
            for date_label, date_value in tender_dates.items():
                if date_value:
                    formatted_label = date_label.replace('_', ' ').title()
                    email_body += f"{formatted_label}: {date_value}<br>"
            
            email_body += "</p><hr>"
        email_body += f"Fount {count} new tenders for {dept}<br>"
    
    email_body += "</body></html>"
    logger.info("Email body constructed. Now sending email.")
    send_email(email_body)
    logger.info("Tender scraper finished.")
