"""Keep ASGI imports read-only; initialize the application on first use."""
import asyncio


class LazyApplication:
    def __init__(self, factory):
        self.factory = factory
        self.application = None
        self.lock = asyncio.Lock()

    async def __call__(self, scope, receive, send):
        if self.application is None:
            async with self.lock:
                if self.application is None:
                    self.application = await asyncio.to_thread(self.factory)
        await self.application(scope, receive, send)
