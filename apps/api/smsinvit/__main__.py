import os
import uvicorn
if __name__=='__main__':
    uvicorn.run('smsinvit.app:create_app',factory=True,host=os.environ.get('SMSINVIT_BIND_HOST','127.0.0.1'),port=int(os.environ.get('PORT','3005')),workers=1,proxy_headers=False,access_log=False)
