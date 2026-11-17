from src.services import studies,runs,joint_runs
# Controllers keep endpoint contracts separate from persistence and calculation.
def overview(db): return {'studies':studies.list_studies(db),'runs':runs.list_runs(db),'joint_runs':joint_runs.list_runs(db)}
def create_study(db,data,user): return studies.create(db,data,user)
def list_rows(db,id): return studies.observations(db,id)
def save_row(db,id,data,user,row_id=None): return studies.save_observation(db,id,data,user,row_id)
def create_run(db,data,user): return runs.create(db,data,user)
def review_run(db,id,data,user): return runs.approve(db,id,data,user)
def create_joint_run(db,data,user): return joint_runs.create(db,data,user)
def list_joint_runs(db): return joint_runs.list_runs(db)
def get_joint_run(db,id): return joint_runs.get(db,id)
