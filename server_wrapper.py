from server import app, ACCESS_KEY
from live_setup import register_live_setup

register_live_setup(app, ACCESS_KEY)
