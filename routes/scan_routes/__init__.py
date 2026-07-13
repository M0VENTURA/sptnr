
from quart import Blueprint

scans_bp = Blueprint("scans", __name__)

# ✅ IMPORT ALL ROUTE FILES HERE
from . import api
from . import artist_album
from . import control
from . import essentia
from . import mp3_import
from . import popularity
