from sqlalchemy import create_engine
from crucible.adapters.persistence.unit_of_work import SqlUnitOfWorkFactory
from crucible.adapters.persistence import migrate

url = "sqlite:///:memory:"
engine = create_engine(url)
migrate.upgrade(url)
