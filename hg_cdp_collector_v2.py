#!/usr/bin/env python3
"""
HG CDP Collector — Pure HTTP login + CDP page XHR polling.
Working format: showtype=live, specialClick= (empty), rtype=rb, ltype=3
No CheckEMNU when specialClick is empty and top.specialClick is not set.
When live games exist (afternoon/evening BJT), dataCount > 0 and IOR tags appear.

Usage: python hg_cdp_collector_v2.py [--interval N] [--output-dir DIR]
"""

import requests, json, websocket, time, re, sys, os, base64
import xml.etree.ElementTree as ET

USER = 'ximeimei1'
PASS = 'Aa123456'
VER = 'b27b446edf512037017ecb25371b6de1_1791436544038'
CDP_URL = 'http://127.0.0.1:9222'
BASE_URL = 'https://www.hga050.com'
POLL_INTERVAL = 5
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), 'runtime')

HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
PROXIES = {'http': None, 'https': None}


class HgCdpCollector:
    def __init__(self):
        self.uid = ''
        self.http_session = None
        self.cdp_ws = None
        self.page = None
        self.success_count = 0
        self.fail_count = 0
        self.last_error = ''
        self.last_xml = ''

    def http_login(self):
        """Login via transform_nl.php to get fresh uid."""
        s = requests.Session()
        s.trust_env = False
        s.proxies.update(PROXIES)
        s.get(BASE_URL + '/', headers=HEADERS)

        ua_b64 = base64.b64encode(HEADERS['User-Agent'].encode()).decode()
        h_post = dict(HEADERS)
        h_post['Content-Type'] = 'application/x-www-form-urlencoded'
        h_post['Origin'] = BASE_URL
        h_post['Referer'] = BASE_URL + '/'

        login_body = f'p=chk_login&langx=zh-cn&ver={VER}&username={USER}&password={PASS}&app=N&auto=DGAZDF&userAgent={ua_b64}'
        r = s.post(BASE_URL + '/transform_nl.php', data=login_body, headers=h_post, timeout=15)
        uid_match = re.search(r'<uid>([^<]+)</uid>', r.text)
        if uid_match:
            self.uid = uid_match.group(1)
        else:
            self.last_error = f'Login failed: {r.text[:200]}'
            return False

        mem_body = f'p=memSet&ver={VER}&uid={self.uid}&langx=zh-cn&action=check'
        s.post(BASE_URL + '/transform_nl.php', data=mem_body, headers=h_post, timeout=10)
        self.http_session = s
        return True

    def find_cdp_page(self):
        """Find the HG page in CDP browser."""
        try:
            r = requests.get(CDP_URL + '/json', timeout=5)
            pages = r.json()
            for p in pages:
                url = p.get('url', '')
                if any(d in url for d in ['hga050', 'crw066', 'hga039', 'hga030', 'mos033']):
                    self.page = p
                    return True
        except Exception as e:
            self.last_error = f'CDP find failed: {e}'
        return False

    def cdp_login(self):
        """Ensure the CDP page is logged in."""
        if not self.page:
            return False

        ws = websocket.create_connection(self.page['webSocketDebuggerUrl'], timeout=10)
        ws.settimeout(8)

        # Check login cookie
        ws.send(json.dumps({'id': 1, 'method': 'Network.getAllCookies'}))
        time.sleep(2)

        cookies = []
        dl = time.time() + 5
        while time.time() < dl:
            try:
                ws.settimeout(0.5)
                msg = json.loads(ws.recv())
                if msg.get('id') == 1:
                    cookies = msg.get('result', {}).get('cookies', [])
                    break
            except:
                pass

        ws.close()

        login_val = ''
        for c in cookies:
            if c['name'].startswith('login_'):
                login_val = c['value']

        if login_val:
            # Already logged in
            return True

        # Need to login via CDP
        ws2 = websocket.create_connection(self.page['webSocketDebuggerUrl'], timeout=10)
        ws2.settimeout(8)

        js_fill = f"document.getElementById('usr').value='{USER}';"
        js_fill += f"document.getElementById('pwd').value='{PASS}';"
        js_fill += "var b=document.getElementById('btn_login');"
        js_fill += "var e=new MouseEvent('click',{bubbles:true,cancelable:true,view:window});"
        js_fill += "b.dispatchEvent(e);'dispatched'"

        ws2.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate',
            'params': {'expression': js_fill, 'timeout': 10000}}))
        time.sleep(8)
        ws2.close()
        return True

    def fetch_game_xml(self, special_click=False):
        """Make get_game_list XHR from within CDP page context."""
        if not self.page or not self.uid:
            return None

        ws = websocket.create_connection(self.page['webSocketDebuggerUrl'], timeout=10)
        ws.settimeout(10)

        ts = int(time.time() * 1000)
        chgSortTS = ts + 6000

        sc = 'special' if special_click else ''
        body = f'uid={self.uid}&ver={VER}&langx=zh-cn&p=get_game_list&p3type=&date=&gtype=ft&showtype=live&rtype=rb&ltype=3&filter=&cupFantasy=N&sorttype=L&specialClick={sc}&isFantasy=N&ts={ts}&chgSortTS={chgSortTS}'

        js_xhr = (
            "(function(){"
            "var x=new XMLHttpRequest();"
            "x.open('POST','/transform.php',false);"
            "x.setRequestHeader('Content-Type','application/x-www-form-urlencoded');"
            "x.send('" + body + "');"
            "return x.responseText;"
            "})()"
        )

        ws.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate',
            'params': {'expression': js_xhr, 'timeout': 25000}}))

        xml_text = ''
        dl = time.time() + 30
        while time.time() < dl:
            try:
                ws.settimeout(2)
                msg = json.loads(ws.recv())
                if msg.get('id') == 1:
                    result = msg.get('result', {})
                    if 'result' in result:
                        xml_text = result['result'].get('value', '')
                    elif 'exceptionDetails' in result:
                        self.last_error = f"JS Exception: {json.dumps(result['exceptionDetails'])[:200]}"
                    break
            except Exception as e:
                if 'timed' not in str(e).lower() and 'timeout' not in str(e).lower():
                    self.last_error = f'WS error: {str(e)[:150]}'

        ws.close()

        if xml_text and len(xml_text) > 50 and '<serverresponse' in xml_text:
            is_err = 'CheckEMNU' in xml_text or ('<code>error</code>' in xml_text) or ('code_type error' in xml_text)
            if is_err:
                self.last_error = f'API error: {xml_text[:150]}'
                return None
            return xml_text
        return None

    def poll_once(self):
        """One polling cycle."""
        xml = self.fetch_game_xml(special_click=False)
        if xml:
            self.last_xml = xml
            self.success_count += 1
            # Save latest
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            with open(os.path.join(OUTPUT_DIR, 'hg_live_latest.xml'), 'w', encoding='utf-8') as f:
                f.write(xml)

            # Parse summary
            games = xml.count('<GID>')
            ior_tags = set(re.findall(r'IOR_w+', xml))
            data_cnt = re.search(r'<dataCount>(d+)</dataCount>', xml)
            dc = data_cnt.group(1) if data_cnt else '?'
            print(f'  [{self.success_count}] dataCount={dc} games={games} IOR_types={len(ior_tags)}')
            return True
        else:
            self.fail_count += 1
            print(f'  [FAIL {self.fail_count}] {self.last_error[:100]}')
            return False

    def run(self, interval=POLL_INTERVAL):
        """Main polling loop."""
        print('=== HG CDP Collector v2 ===')
        print()

        # Login via HTTP
        print('HTTP login...')
        if not self.http_login():
            print(f'FATAL: {self.last_error}')
            return 1
        print(f'  UID: {self.uid}')

        # Find CDP page
        print('Finding CDP page...')
        if not self.find_cdp_page():
            print('FATAL: No HG page in CDP browser')
            print('  Please open https://www.hga050.com/ in Chrome CDP')
            return 1
        print(f'  Page: {self.page.get("title","?")} {self.page.get("url","")[:60]}')

        # Login CDP page
        print('Ensuring CDP page is logged in...')
        if not self.cdp_login():
            print('FATAL: CDP login failed')
            return 1
        print('  CDP page logged in')

        # Poll loop
        print()
        print(f'Starting poll (interval={interval}s)...')
        print(f'Format: showtype=live, specialClick= (empty), rtype=rb, ltype=3')
        print()

        while True:
            self.poll_once()
            time.sleep(interval)


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--interval', type=int, default=POLL_INTERVAL)
    p.add_argument('--output-dir', default=OUTPUT_DIR)
    p.add_argument('--once', action='store_true')
    args = p.parse_args()

    OUTPUT_DIR = args.output_dir
    c = HgCdpCollector()

    if args.once:
        c.http_login()
        c.find_cdp_page()
        c.cdp_login()
        ok = c.poll_once()
        sys.exit(0 if ok else 1)
    else:
        sys.exit(c.run(args.interval))
