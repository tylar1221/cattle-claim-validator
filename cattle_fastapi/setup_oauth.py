"""
One-time OAuth setup. Run ONCE from cattle_fastapi/.
Creates token_combined.pickle, which google_drive_service.py loads.

You can delete this file after it succeeds.
"""
from google_auth_oauthlib.flow import InstalledAppFlow
import pickle

SCOPES = ["https://www.googleapis.com/auth/drive"]

flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
# port=0: let the OS pick any free port.
# Google will redirect to http://localhost:<port>, which the
# "installed" app type in credentials.json accepts.
creds = flow.run_local_server(port=0)

with open("token_combined.pickle", "wb") as f:
    pickle.dump(creds, f)

print("\n✅ token_combined.pickle created.")
print("   You can delete setup_oauth.py now.")