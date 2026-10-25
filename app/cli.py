"""Local console entry point for the unified agent runtime."""
import argparse
import asyncio
from app.api import app

async def run(user_id,session_id):
    runtime=app.state.runtime
    print('PoreiaGo. Type /exit to quit.')
    while True:
        try:message=input('You > ').strip()
        except (KeyboardInterrupt,EOFError):break
        if message in {'/exit','/quit'}:break
        if not message:continue
        response=await runtime.execute(user_id,session_id,message)
        print('\nPoreiaGo > '+response['answer']+'\n')

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--user',default='local-demo')
    parser.add_argument('--session',default='cli')
    args=parser.parse_args()
    asyncio.run(run(args.user,args.session))

if __name__=='__main__':main()
