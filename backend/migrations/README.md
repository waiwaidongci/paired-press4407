Schema v1 is defined by the SQLAlchemy models and created at startup via Base.metadata.create_all. For this local small application no destructive migration is run. Back up named db-data before modifying schemas.

Schema v2 (current): adds tables `joint_runs` and `joint_shards` for the cross-batch
joint sign-flip test. They are created automatically at startup by create_all on an
existing v1 database; no data in `runs`/`observations` is altered. Single-batch runs
keep their original endpoints, table and result schema.
