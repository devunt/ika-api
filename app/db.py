from sqlalchemy import create_engine
from sqlalchemy.ext.automap import automap_base
from sqlalchemy.orm import sessionmaker
from .conf import settings

engine = create_engine(settings.database_url)
Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = automap_base()
Base.prepare(engine, reflect=True)

Application = Base.classes.Applications
Channel = Base.classes.Channels
