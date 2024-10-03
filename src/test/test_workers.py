import json
import pytest
import asyncio
import pytest_asyncio
from nats_queue.main import Worker, Job, Queue
from nats.aio.client import Client as NATS
from nats.js.client import JetStreamContext as JetStream
import nats
import os

user = os.environ.get("NATS_USER")
password = os.environ.get("NATS_PASSWORD")


@pytest_asyncio.fixture
async def get_nc():
    nc = await nats.connect(
        servers=["nats://localhost:4222"], user=user, password=password
    )
    yield nc

    js = nc.jetstream()
    streams = await js.streams_info()
    stream_names = [stream.config.name for stream in streams]

    for name in stream_names:
        await js.delete_stream(name)

    await nc.close()


@pytest_asyncio.fixture
async def job_delay():
    job = Job(
        queue_name="my_queue",
        name="task_1",
        data={"key": "value"},
        delay=15000,
    )
    return job


@pytest.mark.asyncio
async def test_worker_initialization(get_nc):
    nc = get_nc
    worker = Worker(
        nc,
        topic_name="my_queue",
        concurrency=3,
        rate_limit=(5, 15000),
        processor_callback=process_job,
    )

    assert worker.topic_name == "my_queue"
    assert worker.concurrency == 3
    assert worker.rate_limit == (5, 15000)
    assert worker.max_retries == 3


@pytest.mark.asyncio
async def test_worker_connect_success(get_nc):
    nc = get_nc
    worker = Worker(
        nc,
        topic_name="my_queue",
        concurrency=3,
        rate_limit=(5, 30),
        processor_callback=process_job,
    )

    await worker.connect()
    assert isinstance(worker.nc, NATS)
    assert isinstance(worker.js, JetStream)


@pytest.mark.asyncio
async def test_worker_connect_faild():

    worker = Worker(
        "nc",
        topic_name="my_queue",
        concurrency=3,
        rate_limit=(5, 30),
        processor_callback=process_job,
    )
    with pytest.raises(Exception):
        await worker.connect()


@pytest.mark.asyncio
async def test_worker_connect_close_success():
    nc = await nats.connect(
        servers=["nats://localhost:4222"], user=user, password=password
    )
    worker = Worker(
        nc,
        topic_name="my_queue",
        concurrency=3,
        rate_limit=(5, 30),
        processor_callback=process_job,
    )

    await worker.connect()
    await worker.close()
    assert worker.nc.is_closed


@pytest.mark.asyncio
async def test_worker_fetch_messages_success(get_nc):
    nc = get_nc
    queue = Queue(nc, topic_name="my_queue")
    await queue.connect()

    jobs = [
        Job(queue_name="my_queue", name=f"task_{i}", data={"key": f"value_{i}"})
        for i in range(1, 6)
    ]
    await queue.addJobs(jobs)

    worker = Worker(
        nc,
        topic_name="my_queue",
        concurrency=3,
        rate_limit=(5, 15000),
        processor_callback=process_job,
    )
    await worker.connect()

    sub = await worker.js.pull_subscribe(f"{worker.topic_name}.*.*")

    msgs = await worker.fetch_messages(sub, worker.concurrency)
    assert len(msgs) == worker.concurrency
    fetched_job_data_1 = json.loads(msgs[0].data.decode())
    assert fetched_job_data_1["name"] == "task_1"
    fetched_job_data_2 = json.loads(msgs[1].data.decode())
    assert fetched_job_data_2["name"] == "task_2"


@pytest.mark.asyncio
async def test_worker_process_task_success(get_nc):
    nc = get_nc
    queue = Queue(nc, topic_name="my_queue")
    await queue.connect()
    job_data = {
        "name": "task_1",
        "data": {"key": "value"},
        "retry_count": 0,
    }
    job = Job(queue_name="my_queue", name=job_data["name"], data=job_data["data"])
    await queue.addJob(job)

    worker = Worker(
        nc,
        topic_name="my_queue",
        concurrency=3,
        rate_limit=(5, 15000),
        processor_callback=process_job,
        timeout_fetch=5,
    )
    await worker.connect()
    sub = await worker.js.pull_subscribe(f"{worker.topic_name}.*.*")
    msg = (await worker.fetch_messages(sub, worker.concurrency))[0]
    job_data = json.loads(msg.data.decode())
    assert job_data["name"] == "task_1"

    await worker._process_task(msg)
    msgs = await worker.fetch_messages(sub, worker.concurrency)
    assert msgs is None


@pytest.mark.asyncio
async def test_worker_process_task_with_retry(get_nc):
    nc = get_nc
    queue = Queue(nc, topic_name="my_queue")
    await queue.connect()

    job = Job(queue_name="my_queue", name="task_1", data={"key": "value"}, timeout=20)
    await queue.addJob(job)

    worker = Worker(
        nc,
        topic_name="my_queue",
        concurrency=3,
        rate_limit=(5, 15000),
        processor_callback=process_job_with_error,
    )
    await worker.connect()
    sub = await worker.get_subscriptions()
    msgs = await worker.fetch_messages(sub[0], worker.concurrency)
    assert len(msgs) == 1
    msg = msgs[0]
    await worker._process_task(msg)
    job_data = json.loads(msg.data.decode())
    assert job_data["name"] == "task_1"
    assert job_data["meta"]["retry_count"] == 0
    msgs = await worker.fetch_messages(sub[0], worker.concurrency)
    assert len(msgs) == 1
    msg = msgs[0]
    job_data = json.loads(msg.data.decode())
    assert job_data["name"] == "task_1"
    assert job_data["meta"]["retry_count"] == 1


@pytest.mark.asyncio
async def test_worker_process_task_exceeds_max_retries(get_nc):
    nc = get_nc
    queue = Queue(nc, topic_name="my_queue")
    await queue.connect()

    job = Job(queue_name="my_queue", name="task_1", data={"key": "value"}, timeout=20)
    job.meta["retry_count"] = 4
    await queue.addJob(job)

    worker = Worker(
        nc,
        topic_name="my_queue",
        concurrency=3,
        rate_limit=(5, 15000),
        processor_callback=process_job,
    )
    await worker.connect()
    sub = await worker.get_subscriptions()
    msgs = await worker.fetch_messages(sub[0], worker.concurrency)
    assert len(msgs) == 1
    msg = msgs[0]
    await worker._process_task(msg)
    job_data = json.loads(msg.data.decode())
    assert job_data["meta"]["retry_count"] == 4
    msgs = await worker.fetch_messages(sub[0], worker.concurrency)
    assert msgs is None


@pytest.mark.asyncio
async def test_worker_process_task_with_timeout(get_nc):
    nc = get_nc

    queue = Queue(nc, topic_name="my_queue")
    await queue.connect()

    job = Job(queue_name="my_queue", name="task_1", data={"key": "value"}, timeout=1)
    await queue.addJob(job)

    worker = Worker(
        nc,
        topic_name="my_queue",
        concurrency=3,
        rate_limit=(5, 2000),
        processor_callback=process_job_with_timeout,
    )
    await worker.connect()
    sub = await worker.get_subscriptions()
    msgs = await worker.fetch_messages(sub[0], worker.concurrency)

    assert len(msgs) == 1
    msg = msgs[0]
    job_data = json.loads(msg.data.decode())

    assert job_data["name"] == "task_1"
    assert job_data["meta"]["retry_count"] == 0

    await worker._process_task(msg)
    msgs = await worker.fetch_messages(sub[0], worker.concurrency)
    assert len(msgs) == 1
    msg = msgs[0]

    job_data = json.loads(msg.data.decode())
    assert job_data["name"] == "task_1"
    assert job_data["meta"]["retry_count"] == 1


@pytest.mark.asyncio
async def test_worker_get_subscriptions(get_nc):
    nc = get_nc
    queue = Queue(nc, topic_name="my_queue", priorities=3)
    await queue.connect()

    worker = Worker(
        nc,
        topic_name="my_queue",
        concurrency=3,
        rate_limit=(5, 30),
        processor_callback=process_job,
        priorities=queue.priorities,
    )

    await worker.connect()
    subscriptions = await worker.get_subscriptions()
    worker_name = []
    for sub in subscriptions:
        info = await sub.consumer_info()
        filter_subject = info.config.filter_subject
        worker_name.append(filter_subject)
    assert worker_name == ["my_queue.*.1", "my_queue.*.2", "my_queue.*.3"]


@pytest.mark.asyncio
async def test_worker_fetch_retry(get_nc):
    nc = get_nc
    queue = Queue(nc, topic_name="my_queue", priorities=3)
    await queue.connect()

    jobs = [
        Job(queue_name="my_queue", name=f"task_{i}", data={"key": f"value_{i}"})
        for i in range(1, 6)
    ]
    await queue.addJobs(jobs, 1)

    worker = Worker(
        nc,
        topic_name="my_queue",
        concurrency=3,
        rate_limit=(5, 5000),
        processor_callback=process_job,
        priorities=queue.priorities,
    )

    worker2 = Worker(
        nc,
        topic_name="my_queue",
        concurrency=4,
        rate_limit={"max": 5, "duration": 5000},
        processor_callback=process_job,
        priorities=queue.priorities,
    )

    await worker.connect()
    await worker2.connect()

    sub = await worker.get_subscriptions()
    sub2 = await worker2.get_subscriptions()

    info = await sub[0].consumer_info()
    filter_subject = info.config.filter_subject
    assert filter_subject == "my_queue.*.1"

    msgs = await worker.fetch_messages(sub[0], worker.concurrency)
    messages_worker1_len = len(msgs)
    assert messages_worker1_len == 3

    task_name = [msg.subject for msg in msgs]
    assert task_name == [
        "my_queue.task_1.1",
        "my_queue.task_2.1",
        "my_queue.task_3.1",
    ]
    task_ack = [msg.ack() for msg in msgs]
    asyncio.gather(*task_ack)
    info = await sub2[0].consumer_info()
    filter_subject = info.config.filter_subject
    assert filter_subject == "my_queue.*.1"

    msgs = await worker2.fetch_messages(sub2[0], worker.concurrency)
    messages_worker2_len = len(msgs)
    assert messages_worker2_len == 2

    task_name = [msg.subject for msg in msgs]
    assert task_name == ["my_queue.task_4.1", "my_queue.task_5.1"]

    task_ack = [msg.ack() for msg in msgs]
    asyncio.gather(*task_ack)

    messages_len = await worker.fetch_messages(sub[0], worker.concurrency)
    assert messages_len is None

    messages_len2 = await worker2.fetch_messages(sub2[0], worker.concurrency)
    assert messages_len2 is None


@pytest.mark.asyncio
async def test_worker_planned_time(get_nc, job_delay):
    nc = get_nc
    queue = Queue(nc, topic_name="my_queue")
    await queue.connect()

    worker = Worker(
        nc,
        topic_name="my_queue",
        concurrency=3,
        rate_limit=(5, 5000),
        processor_callback=process_job,
    )

    await worker.connect()
    sub = await worker.get_subscriptions()

    await queue.addJob(job_delay)

    msgs = await worker.fetch_messages(sub[0], worker.concurrency)
    assert len(msgs) == 1

    msg = msgs[0]
    job_data = json.loads(msg.data.decode())
    assert job_data["name"] == "task_1"
    assert job_data["meta"]["retry_count"] == 0

    await worker._process_task(msg)
    await asyncio.sleep(15)

    msgs = await worker.fetch_messages(sub[0], worker.concurrency)
    assert len(msgs) == 1

    msg = msgs[0]
    job_data = json.loads(msg.data.decode())
    assert job_data["name"] == "task_1"
    assert job_data["meta"]["retry_count"] == 0
    assert msg.metadata.num_delivered == 2


@pytest.mark.asyncio
async def test_worker_filter_sub(get_nc):
    nc = get_nc
    queue = Queue(nc, topic_name="my_queue")
    await queue.connect()

    job = Job(queue_name="my_queue", name="task_1", data={"key": "value"}, timeout=15)
    await queue.addJob(job)

    worker = Worker(
        nc,
        topic_name="my_queue",
        concurrency=3,
        rate_limit=(5, 5000),
        processor_callback=process_job,
    )

    worker.start()


async def process_job(job_data):
    await asyncio.sleep(1)


async def process_job_with_timeout(job_data):
    await asyncio.sleep(3)


async def process_job_with_error(job_data):
    raise Exception("Test Error")
