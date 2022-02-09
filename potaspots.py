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
potaapi = 'https://api.pota.app/spot/activator/'

consumer = ''
consumer_sec = ''
access = ''
access_sec = ''

api = Twython(consumer, consumer_sec, access, access_sec)

now = datetime.datetime.now()

try:
    spotobj = urllib.request.urlopen(potaapi)
    spotdata = json.loads(spotobj.read())
except Exception as e:
    print(f'Error: {e} {now}')
    sys.exit(-1)

try:
    with open('lastid.pkl', mode='rb') as f:
        lastid = pickle.load(f)
except Exception as e:
    lastid = 2036170

previd = lastid

if spotdata:
    for s in spotdata[::-1]:
        spotid = int(s['spotId'])
        if spotid > lastid:
            lastid = spotid
            ref = s['reference']
            activator = s['activator']
            freq = s['frequency']
            mode = s['mode']
            park = s['name']
            loc  = s['locationDesc']
            spotter = s['spotter']
            comment = s['comments']
            lat = s['latitude']
            lon = s['longitude']
            hhmm= datetime.datetime.fromisoformat(s['spotTime']).strftime('%H:%M')
            mesg = f'{hhmm} {activator} on {ref}({loc} {park}) {freq} {mode} {comment}[{spotter}]'

            m = re.match(prefix, ref)
            if m:
                try:
                    api.update_status(status=mesg, lat=lat, long=lon)
                    print(f'Spotted: {mesg} {now}')
                except TwythonError as e:
                    print(e)

    if previd != lastid:
        print(f'Latest spotid:{lastid} {now}')

    with open('lastid.pkl', mode='wb') as f:
        pickle.dump(lastid, f)
else:
    print(f'No Spots: {now}')
