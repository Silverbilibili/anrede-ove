#!/usr/bin/env python3
"""HG Collector via CDP — login + game data XHR from page context.
Pure HTTP login is used to get the initial uid; then all game data
calls originate from within the Chrome CDP page context to satisfy
cookie/session checks (CheckEMNU)."""

import requests, json, websocket, time, re, sys, os
import xml.etree.ElementTree as ET

USER = 'ximeimei1'
PASS = 'Aa123456'
VER = 'b27b446edf512037017ecb25371b6de1_1791436544038'
CDP_URL = 'http://127.0.0.1:9222'
BASE_URL = 'https://www.hga050.com'
POLL_INTERVAL = 5   # seconds between polls

HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
PROXIES = {'http': None, 'https': None}


def find_hg_page():
    """Find the HG page in CDP browser."""
    try:
        r = requests.get(CDP_URL + '/json', timeout=5)
        pages = r.json()
        for p in pages:
            url = p.get('url', '')
            if 'hga050' in url or 'crw066' in url or 'hga039' in url or 'hga030' in url or 'mos033' in url:
                return p
    except:
        pass
    return None


def login_via_cdp(page):
    """Login to HG via CDP MouseEvent dispatch."""
    ws = websocket.create_connection(page['webSocketDebuggerUrl'], timeout=10)
    ws.settimeout(8)
    
    # Check if login form exists
    ws.send(json.dumps({'id':1, 'method':'Runtime.evaluate',
        'params':{'expression': "!!document.getElementById('usr') && !!document.getElementById('btn_login')"}}))
    time.sleep(2)
    
    has_form = False
    dl = time.time() + 5
    while time.time() < dl:
        try:
            ws.settimeout(0.5)
            msg = json.loads(ws.recv())
            if msg.get('id') == 1:
                has_form = str(msg['result']['result']['value']).lower() == 'true'
                break
        except: pass
    
    if not has_form:
        print('No login form - page may already be logged in')
        ws.close()
        return True
    
    # Fill and dispatch click
    js_fill = "document.getElementById('usr').value='" + USER + "';"
    js_fill += "document.getElementById('pwd').value='" + PASS + "';"
    js_fill += "var b=document.getElementById('btn_login');"
    js_fill += "var e=new MouseEvent('click',{bubbles:true,cancelable:true,view:window});"
    js_fill += "b.dispatchEvent(e);'dispatched'"
    
    ws.send(json.dumps({'id':2, 'method':'Runtime.evaluate', 'params':{'expression': js_fill, 'timeout': 10000}}))
    time.sleep(8)
    
    # Check if login cookie updated
    ws.send(json.dumps({'id':3, 'method':'Network.getAllCookies'}))
    time.sleep(2)
    
    login_val = ''
    dl = time.time() + 5
    while time.time() < dl:
        try:
            ws.settimeout(0.5)
            msg = json.loads(ws.recv())
            if msg.get('id') == 3:
                cookies = msg.get('result',{}).get('cookies',[])
                for c in cookies:
                    if c['name'].startswith('login_'):
                        login_val = c['value']
                break
        except: pass
    
    print(f'Login cookie value: {login_val[:30]}...' if login_val else 'No login cookie found')
    ws.close()
    return bool(login_val)


def fetch_game_xml(page, uid, showtype='live', ltype=3, special_click=True):
    """Make get_game_list XHR from within the CDP page context."""
    ws = websocket.create_connection(page['webSocketDebuggerUrl'], timeout=10)
    ws.settimeout(10)
    
    ts = int(time.time() * 1000)
    
    if special_click:
        body = f'uid={uid}&ver={VER}&langx=zh-cn&p=get_game_list&ts={ts}&gtype=ft&showtype={showtype}&rtype=r&ltype={ltype}&specialClick=special'
    else:
        body = f'uid={uid}&ver={VER}&langx=zh-cn&p=get_game_list&ts={ts}&gtype=ft&showtype={showtype}&rtype=r&ltype={ltype}'
    
    # Build JS: synchronous XHR to transform.php
    js_xhr = (
        "(function(){"
        "var x=new XMLHttpRequest();"
        "x.open('POST','/transform.php',false);"
        "x.setRequestHeader('Content-Type','application/x-www-form-urlencoded');"
        "x.send('" + body + "');"
        "return x.responseText;"
        "})()"
    )
    
    ws.send(json.dumps({'id':1, 'method':'Runtime.evaluate',
        'params':{'expression': js_xhr, 'timeout': 20000}}))
    
    # Read result
    dl = time.time() + 25
    xml_text = ''
    while time.time() < dl:
        try:
            ws.settimeout(2)
            msg = json.loads(ws.recv())
            if msg.get('id') == 1:
                result = msg.get('result', {})
                if 'result' in result:
                    xml_text = result['result'].get('value', '')
                elif 'exceptionDetails' in result:
                    exc = json.dumps(result['exceptionDetails'], indent=2)
                    print(f'JS Exception: {exc[:300]}')
                break
        except Exception as e:
            estr = str(e)
            if 'timed' not in estr.lower() and 'timeout' not in estr.lower():
                print(f'WS error: {estr[:150]}')
    
    ws.close()
    
    if xml_text and len(xml_text) > 50 and '<game ' in xml_text:
        return xml_text
    elif xml_text:
        # Check for error
        if 'CheckEMNU' in xml_text:
            print('XHR returned CheckEMNU')
        elif '<code>error</code>' in xml_text:
            print('XHR returned error:', xml_text[:200])
        else:
            print('XHR returned non-game XML:', xml_text[:300])
    return None


def main():
    print('=== HG CDP Collector ===')
    
    # 1. Find HG page
    page = find_hg_page()
    if not page:
        print('ERROR: No HG page in CDP browser')
        print('Please open https://www.hga050.com/ in the Chrome window')
        return 1
    
    print(f'HG page: {page.get("title","?")} {page.get("url","")[:80]}')
    
    # 2. Login if needed
    if not login_via_cdp(page):
        print('ERROR: Login failed')
        return 1
    
    print('Login OK')
    
    # 3. Get uid from page context
    # First try: make a p=chk_login from page context? No - uid is set by login
    # Instead, use HTTP login to get uid (already known to work)
    import base64
    s = requests.Session()
    s.trust_env = False
    s.proxies.update(PROXIES)
    s.get(BASE_URL + '/', headers=HEADERS)
    
    ua_b64 = base64.b64encode(HEADERS['User-Agent'].encode()).decode()
    login_body = f'p=chk_login&langx=zh-cn&ver={VER}&username={USER}&password={PASS}&app=N&auto=DGAZDF&userAgent={ua_b64}'
    
    h_post = dict(HEADERS)
    h_post['Content-Type'] = 'application/x-www-form-urlencoded'
    h_post['Origin'] = BASE_URL
    h_post['Referer'] = BASE_URL + '/'
    
    r = s.post(BASE_URL + '/transform_nl.php', data=login_body, headers=h_post)
    uid_match = re.search(r'<uid>([^<]+)</uid>', r.text)
    uid = uid_match.group(1) if uid_match else ''
    
    if not uid:
        print('ERROR: Could not get uid from HTTP login')
        return 1
    
    print(f'Fresh UID: {uid}')
    
    # memSet
    mem_body = f'p=memSet&ver={VER}&uid={uid}&langx=zh-cn&action=check'
    s.post(BASE_URL + '/transform_nl.php', data=mem_body, headers=h_post)
    
    # 4. Fetch game data from page context
    print()
    print('=== Fetching game data (specialClick=special, showtype=live) ===')
    
    xml = fetch_game_xml(page, uid, showtype='live', special_click=True)
    
    if xml:
        games = xml.count('<GID>')
        print(f'SUCCESS: {games} games, {len(xml)} bytes')
        
        # Save
        out_path = os.path.join(os.path.dirname(__file__), 'runtime', 'hg_cdp_collector_output.xml')
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(xml)
        print(f'Saved: {out_path}')
        
        # Also try with showtype=today for more games
        print()
        print('=== Fetching game data (specialClick=special, showtype=today) ===')
        xml2 = fetch_game_xml(page, uid, showtype='today', special_click=True)
        if xml2:
            games2 = xml2.count('<GID>')
            print(f'TODAY: {games2} games, {len(xml2)} bytes')
            
            out_path2 = os.path.join(os.path.dirname(__file__), 'runtime', 'hg_cdp_today_output.xml')
            with open(out_path2, 'w', encoding='utf-8') as f:
                f.write(xml2)
            print(f'Saved: {out_path2}')
        
        # Parse and show sample
        try:
            # Count IOR tags
            ior_tags = set(re.findall(r'IOR_\w+', xml))
            print(f'\nIOR tags found: {len(ior_tags)}')
            for t in sorted(ior_tags):
                print(f'  {t}')
            
            # Sample game
            root = ET.fromstring(xml)
            games_list = list(root.iter('game'))
            if games_list:
                g = games_list[0]
                h = (g.find('TEAM_H') or type('',(),{'text':'?'})).text or '?'
                a = (g.find('TEAM_C') or type('',(),{'text':'?'})).text or '?'
                gid = (g.find('GID') or type('',(),{'text':'?'})).text or '?'
                print(f'\nSample game: {h} vs {a} (GID={gid})')
                # Print all IOR/RATIO tags
                for child in g:
                    tag = child.tag.strip()
                    val = (child.text or '').strip()
                    if (tag.startswith('IOR_') or tag.startswith('RATIO_')) and val:
                        print(f'  {tag} = {val}')
        except Exception as e:
            print(f'Parse error: {str(e)[:200]}')
        
        return 0
    else:
        print('FAILED to fetch game data')
        return 1


if __name__ == '__main__':
    sys.exit(main())
