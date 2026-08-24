# -*- coding: utf-8 -*-
"""Toegangspunt voor een echte webserver (Passenger, gunicorn, uWSGI …).

Zo'n server start `app.py` niet zelf op; hij importeert enkel het WSGI-object
en praat daar rechtstreeks mee. Wijs de server dus naar dit bestand:

    gunicorn  : gunicorn --workers 2 --bind 127.0.0.1:8000 wsgi:application
    Passenger : zet "Application Startup File" op wsgi.py
    uWSGI     : --module wsgi:application

Vergeet de omgevingsvariabelen niet (zie het hoofdstuk "Online zetten" in
README.md): TIJDZONE, HTTPS en ACHTER_PROXY.
"""

from app import app as application

# Sommige servers zoeken naar 'app' in plaats van 'application'.
app = application

if __name__ == "__main__":
    from app import start
    start()
