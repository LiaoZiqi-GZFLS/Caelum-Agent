import os
import asyncio
import yaml
from openai import AsyncOpenAI

os.chdir('..')
with open('config.yaml', 'r', encoding='utf-8') as f:
    cfg = yaml.safe_load(f)
api_key = cfg['llm']['api_key']

async def main():
    client = AsyncOpenAI(api_key=api_key, base_url='https://api.moonshot.cn/v1')
    for model in ['kimi-k2-6', 'kimi-k2-0711-preview', 'moonshot-v1-128k', 'kimi-latest']:
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[{'role': 'user', 'content': 'hello'}],
                max_tokens=10
            )
            print(f'{model}: OK - {resp.choices[0].message.content}')
        except Exception as e:
            err = getattr(e, 'body', None)
            msg = err.get('error', {}).get('message', str(e)[:80]) if err else str(e)[:80]
            print(f'{model}: {type(e).__name__} - {msg}')

asyncio.run(main())
