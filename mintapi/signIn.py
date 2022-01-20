from datetime import datetime
import email
import email.header
from email.parser import Parser
import imaplib
import json
import io
import logging
import os
import re
import requests
import subprocess
import sys
import time
import zipfile
import warnings


from selenium.common.exceptions import (
    ElementNotInteractableException,
    ElementNotVisibleException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver import ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from seleniumrequests import Chrome
from selenium.webdriver import ActionChains

import oathtool

logger = logging.getLogger("mintapi")

MFA_VIA_SOFT_TOKEN = "soft_token"
MFA_VIA_EMAIL = "email"
MFA_VIA_SMS = "sms"
MFA_METHOD_LABEL = "mfa_method"
INPUT_CSS_SELECTORS_LABEL = "input_css_selectors"
SPAN_CSS_SELECTORS_LABEL = "span_css_selectors"
BUTTON_CSS_SELECTORS_LABEL = "button_css_selectors"
MFA_PROMT_TEXT = "mfa_promt_text"
PAGE_PROMT_TEXT="Text match on page that will tell us what function to use"
PAGE_METHOD=""

SIGNIN_PAGES = [
    {
        PAGE_METHOD: "password",
        PAGE_PROMT_TEXT: "//*[text()='Enter your Intuit password']", 
    },
    {
        PAGE_METHOD: "multiple",
        PAGE_PROMT_TEXT: "//*[text()='We found your accounts']",
    },
    {
        PAGE_METHOD: MFA_VIA_EMAIL,
        PAGE_PROMT_TEXT: "//*[text()='Check your email']",
        MFA_METHOD_LABEL: MFA_VIA_EMAIL,
        INPUT_CSS_SELECTORS_LABEL: "input[pattern='[0-9]*']",
        SPAN_CSS_SELECTORS_LABEL: '[data-testid="VerifyOtpHeaderText"]',
        BUTTON_CSS_SELECTORS_LABEL: '#ius-mfa-otp-submit-btn, [data-testid="VerifyOtpSubmitButton"]',
    },
    {
        PAGE_METHOD: MFA_VIA_SOFT_TOKEN,
        PAGE_PROMT_TEXT: "//*[text()='Enter your verification code']",
        MFA_METHOD_LABEL: MFA_VIA_SOFT_TOKEN,
        INPUT_CSS_SELECTORS_LABEL: '#iux-mfa-soft-token-verification-code, #ius-mfa-soft-token, [data-testid="VerifySoftTokenInput"]',
        SPAN_CSS_SELECTORS_LABEL: "",
        BUTTON_CSS_SELECTORS_LABEL: '#ius-mfa-soft-token-submit-btn, [data-testid="VerifySoftTokenSubmitButton"]',
    },
    {
        PAGE_METHOD: MFA_VIA_SMS,
        PAGE_PROMT_TEXT: "//*[text()='Check your phone']",
        MFA_METHOD_LABEL: MFA_VIA_SMS,
        INPUT_CSS_SELECTORS_LABEL: "#ius-mfa-sms-otp-card-challenge, #ius-mfa-confirm-code",
        SPAN_CSS_SELECTORS_LABEL: '[data-testid="VerifyOtpHeaderText"]',
        BUTTON_CSS_SELECTORS_LABEL: '#ius-mfa-otp-submit-btn, [data-testid="VerifyOtpSubmitButton"]',
    },
    {
        PAGE_METHOD: "captcha",
        PAGE_PROMT_TEXT: "//*[text()=\"We need to make sure you're not a robot\"]",
        BUTTON_CSS_SELECTORS_LABEL: "//input[@value='Continue']",
    },    
    
]
DEFAULT_MFA_INPUT_PROMPT = "Please enter your 6-digit MFA code: "

STANDARD_MISSING_EXCEPTIONS = (
    NoSuchElementException,
    StaleElementReferenceException,
    ElementNotVisibleException,
)


def get_email_code(
    imap_account, imap_password, imap_server, imap_folder, debug=False, delete=True
):
    if debug:
        warnings.warn(
            "debug param to get_email_code() is deprecated and will be "
            "removed soon; use: logging.getLogger('mintapi')"
            ".setLevel(logging.DEBUG) to show DEBUG log messages.",
            DeprecationWarning,
        )
    code = None
    try:
        imap_client = imaplib.IMAP4_SSL(imap_server)
    except imaplib.IMAP4.error:
        raise RuntimeError("Unable to establish IMAP Client")

    try:
        rv, data = imap_client.login(imap_account, imap_password)
    except imaplib.IMAP4.error:
        raise RuntimeError("Unable to login to IMAP Email")

    code = ""
    for c in range(20):
        time.sleep(10)
        rv, data = imap_client.select(imap_folder)
        if rv != "OK":
            raise RuntimeError("Unable to open mailbox: " + rv)

        rv, data = imap_client.search(None, "ALL")
        if rv != "OK":
            raise RuntimeError("Unable to search the Email folder: " + rv)

        count = 0
        for num in data[0].split()[::-1]:
            count = count + 1
            if count > 3:
                break
            rv, data = imap_client.fetch(num, "(RFC822)")
            if rv != "OK":
                raise RuntimeError("Unable to complete due to error message: " + rv)

            msg = email.message_from_bytes(data[0][1])

            x = email.header.make_header(email.header.decode_header(msg["Subject"]))
            subject = str(x)
            logger.debug("DEBUG: SUBJECT:", subject)

            x = email.header.make_header(email.header.decode_header(msg["From"]))
            frm = str(x)
            logger.debug("DEBUG: FROM:", frm)

            if not re.search("do_not_reply@intuit.com", frm, re.IGNORECASE):
                continue

            if not re.search("Your Mint Account", subject, re.IGNORECASE):
                continue

            date_tuple = email.utils.parsedate_tz(msg["Date"])
            if date_tuple:
                local_date = datetime.fromtimestamp(email.utils.mktime_tz(date_tuple))
            else:
                logger.error("ERROR: FAIL0")

            diff = datetime.now() - local_date

            logger.debug("DEBUG: AGE:", diff.seconds)

            if diff.seconds > 180:
                continue
            code = ""
            logger.debug("DEBUG: EMAIL HEADER OK")
            # get the email body
            body = msg.get_payload(decode=True).decode()
            if body:
                p=re.search(r"Verification code:<.*?(\d\d\d\d\d\d)",body,re.S)
                if p:
                    code = p.group(1)
            logger.debug("DEBUG: CODE FROM EMAIL:", code)

            if code != "":
                break

        logger.debug("DEBUG: CODE FROM EMAIL 2:", code)

        if code != "":
            logger.debug("DEBUG: CODE FROM EMAIL 3:", code)

            if delete and count > 0:
                imap_client.store(num, "+FLAGS", "\\Deleted")

            if delete:
                imap_client.expunge()

            break

    imap_client.logout()
    return code

CHROME_DRIVER_BASE_URL = "https://chromedriver.storage.googleapis.com/"
CHROME_DRIVER_DOWNLOAD_PATH = "{version}/chromedriver_{arch}.zip"
CHROME_DRIVER_LATEST_RELEASE = "LATEST_RELEASE"
CHROME_ZIP_TYPES = {
    "linux": "linux64",
    "linux2": "linux64",
    "darwin": "mac64",
    "win32": "win32",
    "win64": "win32",
}
version_pattern = re.compile(
    "(?P<version>(?P<major>\\d+)\\.(?P<minor>\\d+)\\."
    "(?P<build>\\d+)\\.(?P<patch>\\d+))"
)


def get_chrome_driver_url(version, arch):
    return CHROME_DRIVER_BASE_URL + CHROME_DRIVER_DOWNLOAD_PATH.format(
        version=version, arch=CHROME_ZIP_TYPES.get(arch)
    )


def get_chrome_driver_major_version_from_executable(local_executable_path):
    # Note; --version works on windows as well.
    # check_output fails if running from a thread without a console on win10.
    # To protect against this use explicit pipes for STDIN/STDERR.
    # See: https://github.com/pyinstaller/pyinstaller/issues/3392
    with open(os.devnull, "wb") as devnull:
        version = subprocess.check_output(
            [local_executable_path, "--version"], stderr=devnull, stdin=devnull
        )
        version_match = version_pattern.search(version.decode())
        if not version_match:
            return None
        return version_match.groupdict()["major"]


def get_latest_chrome_driver_version():
    """Returns the version of the latest stable chromedriver release."""
    latest_url = CHROME_DRIVER_BASE_URL + CHROME_DRIVER_LATEST_RELEASE
    latest_request = requests.get(latest_url)

    if latest_request.status_code != 200:
        raise RuntimeError(
            "Error finding the latest chromedriver at {}, status = {}".format(
                latest_url, latest_request.status_code
            )
        )
    return latest_request.text


def get_stable_chrome_driver(download_directory=os.getcwd()):
    chromedriver_name = "chromedriver"
    if sys.platform in ["win32", "win64"]:
        chromedriver_name += ".exe"

    local_executable_path = os.path.join(download_directory, chromedriver_name)

    latest_chrome_driver_version = get_latest_chrome_driver_version()
    version_match = version_pattern.match(latest_chrome_driver_version)
    latest_major_version = None
    if not version_match:
        logger.error(
            "Cannot parse latest chrome driver string: {}".format(
                latest_chrome_driver_version
            )
        )
    else:
        latest_major_version = version_match.groupdict()["major"]
    if os.path.exists(local_executable_path):
        major_version = get_chrome_driver_major_version_from_executable(
            local_executable_path
        )
        if major_version == latest_major_version or not latest_major_version:
            # Use the existing chrome driver, as it's already the latest
            # version or the latest version cannot be determined at the moment.
            return local_executable_path
        logger.info("Removing old version {} of Chromedriver".format(major_version))
        os.remove(local_executable_path)

    if not latest_chrome_driver_version:
        logger.critical(
            "No local chrome driver found and cannot parse the latest chrome "
            "driver on the internet. Please double check your internet "
            "connection, then ask for assistance on the github project."
        )
        return None
    logger.info(
        "Downloading version {} of Chromedriver".format(latest_chrome_driver_version)
    )
    zip_file_url = get_chrome_driver_url(latest_chrome_driver_version, sys.platform)
    request = requests.get(zip_file_url)

    if request.status_code != 200:
        raise RuntimeError(
            "Error finding chromedriver at {}, status = {}".format(
                zip_file_url, request.status_code
            )
        )

    zip_file = zipfile.ZipFile(io.BytesIO(request.content))
    zip_file.extractall(path=download_directory)
    os.chmod(local_executable_path, 0o755)
    return local_executable_path


def _create_web_driver_at_mint_com(
    headless=False,
    session_path=None,
    use_chromedriver_on_path=False,
    chromedriver_download_path=os.getcwd(),
):
    """
    Handles starting a web driver at mint.com
    """
    chrome_options = ChromeOptions()
    if headless:
        chrome_options.add_argument("headless")
        chrome_options.add_argument("no-sandbox")
        chrome_options.add_argument("disable-dev-shm-usage")
        chrome_options.add_argument("disable-gpu")
        # chrome_options.add_argument("--window-size=1920x1080")
    if session_path is not None:
        chrome_options.add_argument("user-data-dir=%s" % session_path)
    chrome_options.add_experimental_option("useAutomationExtension", False)
    chrome_options.add_experimental_option("excludeSwitches",["enable-automation"])
    if use_chromedriver_on_path:
        driver = Chrome(options=chrome_options)
    else:
        driver = Chrome(
            options=chrome_options,
            executable_path=get_stable_chrome_driver(chromedriver_download_path),
        )
    return driver


def get_token(driver: Chrome):
    value_json = driver.find_element_by_name("javascript-user").get_attribute("value")
    return json.loads(value_json)["token"]


def sign_in(
    email,
    password,
    driver,
    mfa_method=None,
    mfa_token=None,
    mfa_input_callback=None,
    intuit_account=None,
    wait_for_sync=True,
    wait_for_sync_timeout=5 * 60,
    imap_account=None,
    imap_password=None,
    imap_server=None,
    imap_folder="INBOX",
):
    """
    Takes in a web driver and gets it through the Mint sign in process
    """
    driver.implicitly_wait(20)  # seconds
    driver.get("https://www.mint.com")
    element = driver.find_element_by_link_text("Sign in")
    element.click()

    WebDriverWait(driver, 20).until(
        expected_conditions.presence_of_element_located(
            (
                By.CSS_SELECTOR,
                "#ius-link-use-a-different-id-known-device, #ius-userid, #ius-identifier, #ius-option-username",
            )
        )
    )
    driver.implicitly_wait(0)  # seconds

    user_selection_page(driver)

    try:  # try to enter in credentials if username and password are on same page
        handle_same_page_username_password(driver, email, password)
    # try to enter in credentials if username and password are on different pages
    except (ElementNotInteractableException, ElementNotVisibleException):
        handle_different_page_username_password(driver, email, password)
        driver.implicitly_wait(20)  # seconds
        password_page(driver, password)

    # Wait until logged in, just in case we need to deal with MFA.
    driver.implicitly_wait(1)  # seconds
    while not driver.current_url.startswith("https://mint.intuit.com/overview.event"):
        bypass_verified_user_page(driver)
        page_type=""
        page_type=search_page_type(driver) # find the page type!
        if page_type == "multiple": # multiple accounts
            account_selection_page(driver, intuit_account)
        elif page_type == "password":
            password_page(driver, password)
        elif page_type == MFA_VIA_SOFT_TOKEN or page_type == MFA_VIA_EMAIL or page_type == MFA_VIA_SMS:                  
            mfa_method = page_type
            mfa_page(
                driver,
                 mfa_method,
                 mfa_token,
                 mfa_input_callback,
                 imap_account,
                 imap_password,
                 imap_server,
                 imap_folder,
            )
        elif page_type == "captcha":
            print("CAPTCHA page detected, Will try to just click the box")
            try:
                driver.switch_to_frame(driver.find_elements_by_tag_name("iframe")[0])
                CheckBox = WebDriverWait(driver, 2).until(
                EC.presence_of_element_located((By.ID ,"recaptcha-anchor"))
                ) 
            # *************  click CheckBox  ***************
            #wait_between(0.5, 0.7)  
                # making click on captcha CheckBox 
                CheckBox.click()
                time.sleep(3)
                driver.switch_to.default_content()
                item = filter(
                lambda x:(x[PAGE_METHOD] == page_type ),SIGNIN_PAGES
                )
                result = list(item)[0]
                button = driver.find_element_by_xpath(
                result[BUTTON_CSS_SELECTORS_LABEL]
              )
                button.click()
                print("CAPTCHA completed!")
            except:
                driver.switch_to.default_content()
                print("Unable to complete CAPTCHA, please complete it manually. Sleeping for 15 seconds")
                time.sleep(15)  
                             
        else:    
            if mfa_method is not None:
                mfa_selection_page(driver, mfa_method)

        driver.implicitly_wait(1)  # seconds

    # Wait until the overview page has actually loaded, and if wait_for_sync==True, sync has completed.
    status_message = None
    if wait_for_sync:
        handle_wait_for_sync(driver, wait_for_sync_timeout)
    return status_message, get_token(driver)


def user_selection_page(driver):
    # click "Use a different user ID" if needed
    try:
        driver.find_element_by_id("ius-link-use-a-different-id-known-device").click()
        WebDriverWait(driver, 20).until(
            expected_conditions.presence_of_element_located(
                (By.CSS_SELECTOR, "#ius-userid, #ius-identifier, #ius-option-username")
            )
        )
    except NoSuchElementException:
        pass


def handle_same_page_username_password(driver, email, password):
    email_input = driver.find_element_by_id("ius-userid")
    if not email_input.is_displayed():
        raise ElementNotVisibleException()
    email_input.clear()  # clear email and user specified email
    email_input.send_keys(email)
    driver.find_element_by_id("ius-password").send_keys(password)
    driver.find_element_by_id("ius-sign-in-submit-btn").submit()


def handle_different_page_username_password(driver, email, password):
    try:
        email_input = driver.find_element_by_id("ius-identifier")
        if not email_input.is_displayed():
            raise ElementNotVisibleException()
        email_input.clear()  # clear email and use specified email
        email_input.send_keys(email)
        driver.find_element_by_id("ius-sign-in-submit-btn").click()
    # click on username if on the saved usernames page
    except (ElementNotInteractableException, ElementNotVisibleException):
        username_elements = driver.find_elements_by_class_name("ius-option-username")
        for username_element in username_elements:
            if username_element.text == email:
                username_element.click()
                break


def bypass_verified_user_page(driver):
    # bypass "Let's add your current mobile number" interstitial page
    try:
        skip_for_now = driver.find_element_by_id("ius-verified-user-update-btn-skip")
        skip_for_now.click()
    except STANDARD_MISSING_EXCEPTIONS:
        pass


def mfa_selection_page(driver, mfa_method):
    try:
        driver.find_element_by_id("ius-mfa-options-form")
        mfa_method_option = driver.find_element_by_id(
            "ius-mfa-option-{}".format(mfa_method)
        )
        mfa_method_option.click()
        mfa_method_submit = driver.find_element_by_id("ius-mfa-options-submit-btn")
        mfa_method_submit.click()
    except STANDARD_MISSING_EXCEPTIONS:
        pass


def mfa_page(
    driver,
    mfa_method,
    mfa_token,
    mfa_input_callback,
    imap_account,
    imap_password,
    imap_server,
    imap_folder,
):
    #mfa_result = search_mfa_method(driver,mfa_method)
    mfa_result = set_mfa_method(driver, mfa_method)
    
    mfa_token_input = mfa_result[0]
    mfa_token_button = mfa_result[1]
    mfa_method = mfa_result[2]

    # mfa screen
    if mfa_method == MFA_VIA_SOFT_TOKEN:
        handle_soft_token(
            driver, mfa_token_input, mfa_token_button, mfa_input_callback, mfa_token
        )
    elif mfa_method == MFA_VIA_EMAIL and imap_account:
        handle_email_by_imap(
            driver,
            mfa_token_input,
            mfa_token_button,
            mfa_input_callback,
            imap_account,
            imap_password,
            imap_server,
            imap_folder,
        )
    else:
        handle_other_mfa(driver, mfa_token_input, mfa_token_button, mfa_input_callback)


def search_mfa_method(driver, mfa_method):
  if not (mfa_method == MFA_VIA_SOFT_TOKEN or mfa_method == MFA_VIA_EMAIL or mfa_method == MFA_VIA_SMS):
      return
  for type in SIGNIN_PAGES:
    mfa_token_input = mfa_token_button = mfa_method = span_text = result = None
    try:
        if mfa_method == type[PAGE_METHOD]:
            mfa_token_input = driver.find_element_by_css_selector(type[INPUT_CSS_SELECTORS_LABEL])
            mfa_token_button = driver.find_element_by_css_selector(type[BUTTON_CSS_SELECTORS_LABEL])
            mfa_method = type[MFA_METHOD_LABEL]
            return mfa_token_input, mfa_token_button, mfa_method            
    except:
      pass
  return

def search_page_type(driver):
  for page in SIGNIN_PAGES:
    try:
      type=driver.find_element_by_xpath(page[PAGE_PROMT_TEXT])
      if type:
        if type.is_displayed():
            page_type= page[PAGE_METHOD]
            return page_type
      else:
        break
    except (NoSuchElementException, ElementNotInteractableException):
      pass
  return "Not found"

def set_mfa_method(driver, mfa_method):
    mfa = filter(
        lambda method:(method[PAGE_METHOD] == mfa_method),SIGNIN_PAGES
    )
    mfa_result = list(mfa)[0]
    try:
        mfa_token_input = driver.find_element_by_css_selector(
            mfa_result[INPUT_CSS_SELECTORS_LABEL]
        )
        mfa_token_button = driver.find_element_by_css_selector(
            mfa_result[BUTTON_CSS_SELECTORS_LABEL]
        )
        mfa_method = mfa_result[MFA_METHOD_LABEL]
    except (NoSuchElementException, ElementNotInteractableException):
        raise RuntimeError("The Multifactor Method supplied is not available.")
    return mfa_token_input, mfa_token_button, mfa_method


def handle_soft_token(
    driver, mfa_token_input, mfa_token_button, mfa_input_callback, mfa_token
):
    try:
        if mfa_token is not None:
            mfa_code = oathtool.generate_otp(mfa_token)
        else:
            mfa_code = (mfa_input_callback or input)(DEFAULT_MFA_INPUT_PROMPT)
        submit_mfa_code(driver, mfa_token_input, mfa_token_button,mfa_code)
    except (NoSuchElementException, ElementNotInteractableException):
        pass


def handle_email_by_imap(
    driver,
    mfa_token_input,
    mfa_token_button,
    mfa_input_callback,
    imap_account,
    imap_password,
    imap_server,
    imap_folder,
):
 #   wait = new WebDriverWait(driver, 120)
    try:
        mfa_code = get_email_code(
            imap_account,
            imap_password,
            imap_server,
            imap_folder,
        )
        if mfa_code:
          submit_mfa_code(driver, mfa_token_input, mfa_token_button,mfa_code)
        else:
          mfa_code = (mfa_input_callback or input)(DEFAULT_MFA_INPUT_PROMPT)
          submit_mfa_code(driver, mfa_token_input, mfa_token_button,mfa_code)    
       
    except (NoSuchElementException, ElementNotInteractableException):
        pass


def handle_other_mfa(driver, mfa_token_input, mfa_token_button, mfa_input_callback):
    try:
        mfa_code = (mfa_input_callback or input)(DEFAULT_MFA_INPUT_PROMPT)
        submit_mfa_code(driver, mfa_token_input, mfa_token_button,mfa_code)
    except (NoSuchElementException, ElementNotInteractableException):
        pass


def submit_mfa_code(driver, mfa_token_input, mfa_token_button,mfa_code):
    mfa_token_input.clear()
    mfa_token_input.send_keys(mfa_code)
    mfa_token_button.click()


def account_selection_page(driver, intuit_account):
    # account selection screen -- if there are multiple accounts, select one
    try:
        select_account = driver.find_element_by_id("ius-mfa-select-account-section")
        if intuit_account is not None:
   
            account_input = select_account.find_element_by_xpath(
                "//span[text()='{}']/preceding::input[1]".format(
                    intuit_account
            )
            )
            action = ActionChains(driver)
            action.move_to_element(account_input).click().perform()            
        mfa_code_submit = driver.find_element_by_css_selector(
            '#ius-sign-in-mfa-select-account-continue-btn, [data-testid="SelectAccountContinueButton"]'
        )
        mfa_code_submit.click()
    except NoSuchElementException:
        pass  # not on account selection screen


def password_page(driver, password):
    # password only sometimes after mfa
    try:
        driver.find_element_by_id(
            "ius-sign-in-mfa-password-collection-current-password"
        ).send_keys(password)
        driver.find_element_by_id(
            "ius-sign-in-mfa-password-collection-continue-btn"
        ).submit()
    except STANDARD_MISSING_EXCEPTIONS:
        pass  # not on secondary mfa password screen


def handle_wait_for_sync(driver, wait_for_sync_timeout):
    try:
        # Status message might not be present straight away. Seems to be due
        # to dynamic content (client side rendering).
        status_web_element = WebDriverWait(driver, 30).until(
            expected_conditions.visibility_of_element_located(
                (By.CSS_SELECTOR, ".SummaryView .message")
            )
        )
        WebDriverWait(driver, wait_for_sync_timeout).until(
            lambda x: "Account refresh complete"
            in status_web_element.get_attribute("innerHTML")
        )
        status_message = status_web_element.text
    except (TimeoutException, StaleElementReferenceException):
        logger.warning(
            "Mint sync apparently incomplete after timeout. "
            "Data retrieved may not be current."
        )


def get_web_driver(
    email,
    password,
    headless=False,
    mfa_method=None,
    mfa_token=None,
    mfa_input_callback=None,
    intuit_account=None,
    wait_for_sync=True,
    wait_for_sync_timeout=5 * 60,
    session_path=None,
    imap_account=None,
    imap_password=None,
    imap_server=None,
    imap_folder="INBOX",
    use_chromedriver_on_path=False,
    chromedriver_download_path=os.getcwd(),
):
    warnings.warn(
        "get_web_driver instance function is going to be deprecated in the next major release"
        "please use login_and_get_token or sign_in",
        DeprecationWarning,
    )
    driver = _create_web_driver_at_mint_com(
        headless, session_path, use_chromedriver_on_path, chromedriver_download_path
    )

    status_message = None
    try:
        status_message, _ = sign_in(
            email,
            password,
            driver,
            mfa_method,
            mfa_token,
            mfa_input_callback,
            intuit_account,
            wait_for_sync,
            wait_for_sync_timeout,
            imap_account,
            imap_password,
            imap_server,
            imap_folder,
        )
    except Exception as e:
        logger.exception(e)
        driver.quit()
        driver = None

    return driver, status_message
