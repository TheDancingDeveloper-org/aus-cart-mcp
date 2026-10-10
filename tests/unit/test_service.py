import asyncio

from aus_cartwatch import service as service_module
from aus_cartwatch.service import Service, default_notifiers


def test_default_notifiers_follow_configuration(monkeypatch):
    for name in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_IDS", "WEBHOOK_URL"):
        monkeypatch.delenv(f"AUS_CARTWATCH_{name}", raising=False)
    assert default_notifiers() == ([], None)
    monkeypatch.setenv("AUS_CARTWATCH_TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("AUS_CARTWATCH_TELEGRAM_CHAT_IDS", "1")
    monkeypatch.setenv("AUS_CARTWATCH_WEBHOOK_URL", "http://hook")
    notifiers, telegram = default_notifiers()
    assert [n.name for n in notifiers] == ["telegram", "webhook"] and telegram is notifiers[0]


async def test_background_tasks_start_and_stop(store, fake, clock, monkeypatch):
    ticks = []

    class Bot:
        async def poll_forever(self, store, handler):
            await asyncio.Event().wait()

    service = Service(store, client_factory=fake.factory(store, clock=clock), notifiers=[], telegram=Bot(), clock=clock)

    async def tick():
        ticks.append(1)
        if len(ticks) == 1:
            raise RuntimeError("a bad tick must not stop the loop")

    monkeypatch.setattr(service, "tick", tick)
    monkeypatch.setattr(service_module, "TICK_SECONDS", 0)
    service.start()
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(ticks) >= 2 and len(service._tasks) == 2
    await service.stop()
    assert service._tasks == []


async def test_scheduler_can_be_disabled(store, fake, clock, monkeypatch):
    monkeypatch.setenv("AUS_CARTWATCH_SCHEDULER", "off")
    service = Service(store, client_factory=fake.factory(store), notifiers=[], clock=clock)
    service.start()
    assert service._tasks == []
    await service.tick()  # a manual tick still works
