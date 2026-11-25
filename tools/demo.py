"""PC demo of the screen page, with SAMPLE data standing in for Home Assistant.

    python tools/demo.py        ->  http://127.0.0.1:8798/arcane_screen_static/screen.html#feed=/feed&act=/act

Taps are printed here. Every 20 s a sample banner is pushed, and the power chip changes every 5 s.

For screenshots, the feed address takes options (encode them inside #feed=, e.g. #feed=%2Ffeed%3Fbuttons%3D1):
    buttons=1         show the big buttons in place of the week
    notice=<name>     show notices under the clock (bin, binred, rain, mix)
    banner=<name>     push one banner straight away (home, delivery, rain, gate, alarm) and no random ones
    still=1           no random banners and no changing numbers
    welcome=<n>       the welcome home screen with n sample cameras (0-3); use act=/demo/act/k so the tiles find /demo/cam/k/<i>
Add &wall=<name> to the page address to pick a wallpaper (eclipse, contours, dusk, ridge, nebula, orbit, lights).
"""
import datetime
import json
import os
import random
import threading
import time
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
WWW = os.path.join(HERE, '..', 'custom_components', 'arcane_screen', 'www')
TYPES = {'.html': 'text/html; charset=utf-8', '.webp': 'image/webp'}
PORT = int(os.environ.get('PORT', '8798'))
BANNERS = {
    'home': {'title': 'Alex is home', 'message': 'Arrived at the front gate', 'icon': 'person', 'seconds': 600, 'tone': 'good'},
    'delivery': {'title': 'Delivery', 'message': 'A parcel was left at the front door', 'icon': 'truck', 'seconds': 600, 'tone': 'good'},
    'rain': {'title': 'Rain soon', 'message': 'The washing is still out', 'icon': 'rain', 'seconds': 600, 'tone': 'warn'},
    'gate': {'title': 'Gate left open', 'message': 'The front gate has been open for 10 minutes', 'icon': 'alert', 'seconds': 600, 'tone': 'alert'},
    'alarm': {'title': 'Alarm armed', 'message': 'Everyone has left, the house is armed', 'icon': 'shieldon', 'seconds': 600, 'tone': 'info'},
    'water': {'title': 'Watering the garden', 'message': 'Back lawn, 20 minutes', 'icon': 'drop', 'seconds': 600, 'tone': 'info'},
    'washer': {'title': 'Washing is done', 'message': 'The washing machine has finished', 'icon': 'check', 'seconds': 600, 'tone': 'good'},
}
NOTICES = {
    'bin': [{'id': 'bin_night', 'text': 'Bins out tonight', 'icon': 'bin', 'level': 'warn', 'tint': True, 'tap_clears': True}],
    'binred': [{'id': 'bin_night', 'text': 'Bins still not out', 'icon': 'bin', 'level': 'alert', 'tint': True, 'tap_clears': True}],
    'rain': [{'id': 'washing', 'text': 'Rain soon, washing is out', 'icon': 'rain', 'level': 'warn', 'tint': False, 'tap_clears': False}],
    'mix': [{'id': 'bin_night', 'text': 'Bins out tonight', 'icon': 'bin', 'level': 'warn', 'tint': False, 'tap_clears': True},
            {'id': 'parcel', 'text': '2 parcels today', 'icon': 'truck', 'level': 'good', 'tint': False, 'tap_clears': False}],
}


def welcome(n):
    now = datetime.datetime.now().astimezone()
    names = ['FRONT GATE', 'FRONT PATH', 'BACK YARD']
    return {'id': 1, 'name': 'Alex', 'message': 'Front gate is opening', 'after': 'Front gate is open',
            'pulse_until': (now + datetime.timedelta(minutes=10)).isoformat(), 'note': 'Closes when the garage door shuts',
            'cams': [{'i': i, 'name': names[i]} for i in range(n)]}


def sample(buttons=False):
    now = datetime.datetime.now().astimezone().replace(minute=0, second=0, microsecond=0)
    conds = ['sunny', 'partlycloudy', 'cloudy', 'rainy', 'clear-night', 'clear-night', 'clear-night', 'cloudy', 'cloudy', 'sunny', 'sunny', 'sunny']
    week = [(14, 31, 'cloudy', 10), (17, 25, 'sunny', 5), (17, 29, 'partlycloudy', 20), (19, 34, 'lightning-rainy', 80), (19, 27, 'rainy', 95), (16, 24, 'sunny', 0)]
    ago = (now - datetime.timedelta(hours=2)).isoformat()
    return {
        'v': 1, 'phase': 'night', 'clock24': False,
        'now': {'temp': 23.4, 'cond': 'partlycloudy', 'hum': 61, 'wind': 14, 'wind_unit': 'km/h'},
        'today': {'low': 17, 'high': 29, 'chance': 10},
        'unit': '°C',
        'hourly': [{'t': (now + datetime.timedelta(hours=i)).isoformat(), 'temp': 24 - i * .8, 'cond': conds[i], 'chance': 60 if i == 3 else 5} for i in range(12)],
        'daily': [{'d': (now.date() + datetime.timedelta(days=i + 1)).isoformat(), 'low': w[0], 'high': w[1], 'cond': w[2], 'chance': w[3], 'mm': 0} for i, w in enumerate(week)],
        'events': [
            {'start': (now + datetime.timedelta(hours=3)).isoformat(), 'title': 'Swimming lesson'},
            {'start': (now.date() + datetime.timedelta(days=1)).isoformat(), 'title': 'Bin night'},
            {'start': (now + datetime.timedelta(days=2, hours=1)).isoformat(), 'title': 'Dinner with friends'},
        ],
        'events_more': 0,
        'chips': [
            {'i': 0, 'name': 'Solar power', 'icon': 'bolt', 'text': '2.4 kW', 'tone': '', 'state': '2400', 'changed': ago, 'actions': []},
            {'i': 1, 'name': 'Alarm', 'icon': 'shieldon', 'text': 'Alarm', 'tone': 'on', 'state': 'armed home', 'changed': ago, 'actions': []},
            {'i': 2, 'name': 'Front gate', 'icon': 'gate', 'gate': 'closed', 'text': 'Front gate', 'tone': '', 'state': 'closed', 'changed': ago,
             'actions': [{'a': 'open', 'label': 'Open'}, {'a': 'close', 'label': 'Close'}]},
            {'i': 3, 'name': 'Garage', 'icon': 'garage', 'text': 'Garage', 'tone': '', 'state': 'closed', 'changed': ago,
             'actions': [{'a': 'open', 'label': 'Open'}, {'a': 'close', 'label': 'Close'}]},
            {'i': 4, 'name': 'Air conditioner', 'icon': 'snow', 'text': 'Air con', 'tone': 'on', 'state': 'cool', 'changed': ago,
             'actions': [{'a': 'off', 'label': 'Turn off'}]},
            {'i': 5, 'name': 'Bedroom', 'icon': 'thermo', 'text': '22.6°', 'tone': '', 'state': '22.6', 'changed': ago, 'actions': []},
            {'i': 6, 'name': 'Back door', 'icon': 'door', 'text': 'Back door', 'tone': 'warn', 'state': 'on', 'changed': ago, 'actions': []},
        ],
        'buttons': [
            {'i': 0, 'name': 'Lounge lamp', 'on': True, 'icon': 'bulb'},
            {'i': 1, 'name': 'Reading lamp', 'on': False, 'icon': 'bulb'},
            {'i': 2, 'name': 'Porch light', 'on': False, 'icon': 'bulb'},
            {'i': 3, 'name': 'Good night', 'on': False, 'icon': 'play'},
        ] if os.environ.get('BUTTONS') or buttons else None,
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(n)
        print('tap:', body.decode(errors='replace'), flush=True)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', '11')
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_GET(self):
        path = self.path.split('?')[0].split('#')[0]
        if path == '/feed':
            return self.feed()
        if '/cam/' in path:                      # welcome screen tiles: sample pictures in tools/demo-cams
            name = 'cam%s.jpg' % path.rsplit('/', 1)[-1]
            full = os.path.join(HERE, 'demo-cams', name)
            if os.path.isfile(full):
                with open(full, 'rb') as fh:
                    body = fh.read()
                self.send_response(200); self.send_header('Content-Type', 'image/jpeg'); self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store'); self.end_headers(); self.wfile.write(body)
                return
        name = os.path.basename(path)
        full = os.path.join(WWW, name)
        if not os.path.isfile(full):
            self.send_response(404); self.send_header('Content-Length', '0'); self.end_headers(); return
        with open(full, 'rb') as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header('Content-Type', TYPES.get(os.path.splitext(full)[1], 'application/octet-stream'))
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def feed(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        banner = BANNERS.get(q.get('banner', ''))
        still = bool(q.get('still') or banner)
        data, n = sample(bool(q.get('buttons'))), 0
        if q.get('welcome', '') in ('0', '1', '2', '3'):
            data['welcome'] = welcome(int(q['welcome']))
            still = True
        if q.get('notice') in NOTICES:
            data['notices'] = NOTICES[q['notice']]
            still = True
        try:
            if banner:
                self.wfile.write(b'data: ' + json.dumps(data, separators=(',', ':')).encode() + b'\n\n')
                self.wfile.write(b'event: banner\ndata: ' + json.dumps(banner).encode() + b'\n\n')
                self.wfile.flush()
            while True:
                self.wfile.write(b'data: ' + json.dumps(data, separators=(',', ':')).encode() + b'\n\n')
                self.wfile.flush()
                time.sleep(5)
                n += 1
                if still:
                    continue
                data['chips'][0]['text'] = '%.1f kW' % random.uniform(0.5, 4.5)
                if n % 4 == 0:
                    b = {'title': 'Delivery', 'message': 'A parcel was left at the front door', 'icon': 'truck', 'seconds': 8, 'tone': 'good'}
                    self.wfile.write(b'event: banner\ndata: ' + json.dumps(b).encode() + b'\n\n')
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            pass


if __name__ == '__main__':
    print(f'http://127.0.0.1:{PORT}/arcane_screen_static/screen.html#feed=/feed&act=/act')
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
