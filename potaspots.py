#!/usr/bin/env python3.9
# coding: utf-8
import datetime
from os import access
import pickle
import urllib.request
import json
import re
import sys
from twython import Twython 

prefix = 'JA-*'
potaapi = 'https://api.pota.us/spot/activator/'

consumer = ''
consumer_sec = ''
access = ''
access_sec = ''

api = Twython(consumer, consumer_sec, access, access_sec)

try:
    spotobj = urllib.request.urlopen(potaapi)
    spotdata = json.loads(spotobj.read())
except Exception as e:
    print(e)
    sys.exit(-1)

try:
    with open('lastid.pkl', mode='rb') as f:
        lastid = pickle.load(f)
except Exception as e:
    lastid = 0 

for s in spotdata[::-1]:
    spotid = int(s['spotId'])
    if spotid > lastid:
        lastid = spotid
        ref = s['reference']
        activator = s['activator']
        freq = s['frequency']
        mode = s['mode']
        park = s['parkName']
        spotter = s['spotter']
        comment = s['comments']
        lat = s['latitude']
        lon = s['longitude']
        hhmm= datetime.datetime.fromisoformat(s['spotTime']).strftime('%H:%M')
        mesg = f'{hhmm} {activator} on {ref}({park}) {freq} {mode} {comment}[{spotter}]'
        
        m = re.match(prefix, ref)
        if m:
            api.update_status(status=mesg, lat=lat, long=lon)            

with open('lastid.pkl', mode='wb') as f:
    pickle.dump(lastid, f)
