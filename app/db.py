from sqlalchemy import create_engine, Column, Integer, String
from sqlalchemy.ext.automap import automap_base
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from .conf import settings

engine = create_engine(settings.database_url)
Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
AutoBase = automap_base()
AutoBase.prepare(engine, reflect=True)

Application = AutoBase.classes.Applications
Channel = AutoBase.classes.Channels
ChannelIntegration = AutoBase.classes.ChannelIntegrations


class SlackInstallation(Base):
    __tablename__ = 'slack_installations'
    id = Column(Integer, primary_key=True)
    team_id = Column(String(255), unique=True)
    bot_user_id = Column(String(255))
    access_token = Column(String(255))


Base.metadata.create_all(engine)
