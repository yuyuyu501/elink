import asyncio
import concurrent.futures
import threading

from PySide6.QtCore import QObject, Signal


class Runtime(QObject):
    completed = Signal(object, object, object)
    message = Signal(str)
    feedback = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.run, name="elink-network", daemon=True)
        self.completed.connect(self.deliver)
        self.thread.start()

    def run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        self.loop.run_until_complete(self.loop.shutdown_default_executor())
        self.loop.close()

    def submit(self, coro, success=None, failure=None):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        def done(result):
            try:
                value = result.result()
            except concurrent.futures.CancelledError:
                return
            except Exception as exc:
                self.completed.emit(failure, str(exc), True)
            else:
                self.completed.emit(success, value, False)
        future.add_done_callback(done)
        return future

    def deliver(self, callback, result, failed):
        if callback:
            callback(result)
        elif failed:
            self.message.emit(str(result))

    def call(self, function, *args):
        self.loop.call_soon_threadsafe(function, *args)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=8)
