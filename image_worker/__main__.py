import os
import uvicorn

from image_worker.service import create_app

os.umask(0o077)
uvicorn.run(create_app(), host="0.0.0.0", port=8311, access_log=False)
