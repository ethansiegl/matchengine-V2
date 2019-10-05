FROM python:3.7

COPY . .
RUN pip install bson>=0.5.8 python-dateutil==2.8.0 PyYAML==5.1 Pandas>=0.25.0 pymongo==3.8.0 networkx==2.3 motor==2.0.0
WORKDIR /
