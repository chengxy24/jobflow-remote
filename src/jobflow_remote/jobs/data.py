from collections import defaultdict
from datetime import datetime, timezone
from enum import Enum
from functools import cached_property
from typing import Optional, Union

from jobflow import Flow, Job, JobStore
from monty.json import jsanitize
from pydantic import BaseModel, Field
from qtoolkit.core.data_objects import QResources, QState

from jobflow_remote.config.base import ExecutionConfig
from jobflow_remote.jobs.state import FlowState, JobState

IN_FILENAME = "jfremote_in.json"
OUT_FILENAME = "jfremote_out.json"


def get_initial_job_doc_dict(
    job: Job,
    parents: Optional[list[str]],
    db_id: int,
    worker: str,
    exec_config: Optional[ExecutionConfig],
    resources: Optional[Union[dict, QResources]],
):
    from monty.json import jsanitize

    # take the resources either from the job, if they are defined
    # (they can be defined dynamically by the update_config) or the
    # defined value
    job_resources = job.config.manager_config.get("resources") or resources
    job_exec_config = job.config.manager_config.get("exec_config") or exec_config
    worker = job.config.manager_config.get("worker") or worker

    job_doc = JobDoc(
        job=jsanitize(job, strict=True, enum_values=True),
        uuid=job.uuid,
        index=job.index,
        db_id=db_id,
        state=JobState.WAITING if parents else JobState.READY,
        parents=parents,
        worker=worker,
        exec_config=job_exec_config,
        resources=job_resources,
    )

    return job_doc.as_db_dict()


def get_initial_flow_doc_dict(flow: Flow, job_dicts: list[dict]):
    jobs = [j["uuid"] for j in job_dicts]
    ids = [(j["db_id"], j["uuid"], j["index"]) for j in job_dicts]
    parents = {j["uuid"]: {"1": j["parents"]} for j in job_dicts}

    flow_doc = FlowDoc(
        uuid=flow.uuid,
        jobs=jobs,
        state=FlowState.READY,
        name=flow.name,
        ids=ids,
        parents=parents,
    )

    return flow_doc.as_db_dict()


class RemoteInfo(BaseModel):
    step_attempts: int = 0
    queue_state: Optional[QState] = None
    process_id: Optional[str] = None
    retry_time_limit: Optional[datetime] = None
    error: Optional[str] = None


class JobInfo(BaseModel):
    uuid: str
    index: int
    db_id: int
    worker: str
    name: str
    state: JobState
    remote: RemoteInfo = RemoteInfo()
    parents: Optional[list[str]] = None
    previous_state: Optional[JobState] = None
    error: Optional[str] = None
    lock_id: Optional[str] = None
    lock_time: Optional[datetime] = None
    run_dir: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    created_on: datetime = datetime.utcnow()
    updated_on: datetime = datetime.utcnow()
    priority: int = 0
    metadata: Optional[dict] = None

    @property
    def is_locked(self) -> bool:
        return self.lock_id is not None

    @property
    def run_time(self) -> Optional[float]:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()

        return None

    @property
    def estimated_run_time(self) -> Optional[float]:
        if self.start_time:
            return (
                datetime.now(tz=self.start_time.tzinfo) - self.start_time
            ).total_seconds()

        return None

    @classmethod
    def from_query_output(cls, d) -> "JobInfo":
        job = d.pop("job")
        for k in ["name", "metadata"]:
            d[k] = job[k]
        return cls.model_validate(d)


def _projection_db_info() -> list[str]:
    projection = list(JobInfo.model_fields.keys())
    projection.remove("name")
    projection.append("job.name")
    projection.append("job.metadata")
    return projection


projection_job_info = _projection_db_info()


class JobDoc(BaseModel):
    # TODO consider defining this as a dict and provide a get_job() method to
    # get the real Job. This would avoid (de)serializing jobs if this document
    # is used often to interact with the DB.
    job: Job
    uuid: str
    index: int
    db_id: int
    worker: str
    state: JobState
    remote: RemoteInfo = RemoteInfo()
    # only the uuid as list of parents for a JobDoc (i.e. uuid+index) is
    # enough to determine the parents, since once a job with a uuid is
    # among the parents, all the index will still be parents.
    # Note that for just the uuid this condition is not true: JobDocs with
    # the same uuid but different indexes may have different parents
    parents: Optional[list[str]] = None
    previous_state: Optional[JobState] = None
    error: Optional[str] = None  # TODO is there a better way to serialize it?
    lock_id: Optional[str] = None
    lock_time: Optional[datetime] = None
    run_dir: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    created_on: datetime = datetime.utcnow()
    updated_on: datetime = datetime.utcnow()
    priority: int = 0
    store: Optional[JobStore] = None
    exec_config: Optional[Union[ExecutionConfig, str]] = None
    resources: Optional[Union[QResources, dict]] = None

    stored_data: Optional[dict] = None
    history: Optional[list[str]] = None  # ?

    def as_db_dict(self):
        # required since the resources are not serialized otherwise
        if isinstance(self.resources, QResources):
            resources_dict = self.resources.as_dict()
        d = jsanitize(
            self.model_dump(mode="python"),
            strict=True,
            allow_bson=True,
            enum_values=True,
        )
        if isinstance(self.resources, QResources):
            d["resources"] = resources_dict
        return d


class FlowDoc(BaseModel):
    uuid: str
    jobs: list[str]
    state: FlowState
    name: str
    lock_id: Optional[str] = None
    lock_time: Optional[datetime] = None
    created_on: datetime = datetime.utcnow()
    updated_on: datetime = datetime.utcnow()
    metadata: dict = Field(default_factory=dict)
    # parents need to include both the uuid and the index.
    # When dynamically replacing a Job with a Flow some new Jobs will
    # be parents of the job with index=i+1, but will not be parents of
    # the job with index i.
    # index is stored as string, since mongodb needs string keys
    parents: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    # ids correspond to db_id, uuid, index for each JobDoc
    ids: list[tuple[int, str, int]] = Field(default_factory=list)
    # jobs_states: dict[str, FlowState]

    def as_db_dict(self):
        d = jsanitize(
            self.model_dump(mode="python"),
            strict=True,
            allow_bson=True,
            enum_values=True,
        )
        return d

    @cached_property
    def int_index_parents(self):
        d = defaultdict(dict)
        for child_id, index_parents in self.parents.items():
            for index, parents in index_parents.items():
                d[child_id][int(index)] = parents
        return dict(d)

    @cached_property
    def children(self) -> dict[str, list[tuple[str, int]]]:
        d = defaultdict(list)
        for job_id, index_parents in self.parents.items():
            for index, parents in index_parents.items():
                for parent_id in parents:
                    d[parent_id].append((job_id, int(index)))

        return dict(d)

    def descendants(self, job_uuid: str) -> list[tuple[str, int]]:
        descendants = set()

        def add_descendants(uuid):
            children = self.children.get(uuid)
            if children:
                descendants.update(children)
                for child in children:
                    add_descendants(child[0])

        add_descendants(job_uuid)

        return list(descendants)

    @cached_property
    def ids_mapping(self) -> dict[str, dict[int, int]]:
        d: dict = defaultdict(dict)

        for db_id, job_id, index in self.ids:
            d[job_id][int(index)] = db_id

        return dict(d)


class RemoteError(RuntimeError):
    def __init__(self, msg, no_retry=False):
        self.msg = msg
        self.no_retry = no_retry


class FlowInfo(BaseModel):
    db_ids: list[int]
    job_ids: list[str]
    job_indexes: list[int]
    flow_id: str
    state: FlowState
    name: str
    updated_on: datetime
    workers: list[str]
    job_states: list[JobState]
    job_names: list[str]

    @classmethod
    def from_query_dict(cls, d):
        # the dates should be in utc time. Convert them to the system time
        updated_on = d["updated_on"]
        updated_on = updated_on.replace(tzinfo=timezone.utc).astimezone(tz=None)
        flow_id = d["uuid"]

        db_ids, job_ids, job_indexes = list(zip(*d["ids"]))

        jobs_data = d.get("jobs_list") or []
        workers = []
        job_states = []
        job_names = []
        for job_doc in jobs_data:
            job_names.append(job_doc["job"]["name"])
            state = job_doc["state"]
            job_states.append(JobState(state))
            workers.append(job_doc["worker"])

        state = FlowState(d["state"])

        return cls(
            db_ids=db_ids,
            job_ids=job_ids,
            job_indexes=job_indexes,
            flow_id=flow_id,
            state=state,
            name=d["name"],
            updated_on=updated_on,
            workers=workers,
            job_states=job_states,
            job_names=job_names,
        )


class DynamicResponseType(Enum):
    REPLACE = "replace"
    DETOUR = "detour"
    ADDITION = "addition"


def get_reset_job_base_dict() -> dict:
    """
    Return a dictionary with the basic properties to update in case of reset.

    Returns
    -------

    """
    d = {
        "remote.step_attempts": 0,
        "remote.retry_time_limit": None,
        "previous_state": None,
        "remote.queue_state": None,
        "remote.error": None,
        "error": None,
        "updated_on": datetime.utcnow(),
        "start_time": None,
        "end_time": None,
    }
    return d
