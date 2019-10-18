FROM python:3.7
RUN apt-get update && apt-get upgrade -y

# set timezone
RUN DEBIAN_FRONTEND=noninteractive apt-get update --fix-missing && \
 apt-get install -y tzdata vim && \
 dpkg-reconfigure -f noninteractive tzdata && \
 mv /etc/localtime /etc/localtime.bak && \
 ln -s /usr/share/zoneinfo/America/New_York /etc/localtime

COPY . .

RUN python setup.py install

WORKDIR /
COPY SECRETS_JSON.json /
ENV SECRETS_JSON=/SECRETS_JSON.json

ENTRYPOINT ["python", "-m", "matchengine.main"]
CMD ["match"]
