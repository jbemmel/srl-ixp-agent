ARG SR_LINUX_RELEASE
# ARG SR_BASEIMG
FROM srl/custombase:$SR_LINUX_RELEASE as base

# Setup env
ENV LANG C.UTF-8
ENV LC_ALL C.UTF-8
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONFAULTHANDLER 1
ENV AGENT_DIR /opt/demo-agents/ixp-agent/

FROM base AS agent-deps

# Need to upgrade pip and setuptools
RUN sudo python3 -m pip install --upgrade pip setuptools

# Install pipenv and compilation dependencies
RUN sudo python3 -m pip install pipenv==2021.11.09

# Install python dependencies in ${AGENT_DIR}/.venv
COPY Pipfile ${AGENT_DIR}

# Lock file is created in a different environment, leave out for now
# COPY Pipfile.lock .
RUN cd ${AGENT_DIR} && \
    sudo PIPENV_VENV_IN_PROJECT=1 /usr/local/bin/pipenv install --deploy --site-packages

FROM base AS runtime

# Copy virtual env from agent-deps stage
# Also includes generated lock file, for versions (included in .rpm)
COPY --from=agent-deps ${AGENT_DIR} ${AGENT_DIR}

ENV PATH="${AGENT_DIR}.venv/bin:$PATH"

# Create and switch to a new user
# RUN useradd --create-home appuser
# WORKDIR /home/appuser
# USER appuser

# Install application into container
COPY src /opt/demo-agents

# run pylint to catch any obvious errors (includes .venv?)
# RUN sudo yum install -y pylint && pip install pylint-protobuf
RUN PYTHONPATH=${AGENT_DIR}.venv/lib/python3.6/site-packages/:$AGENT_PYTHONPATH pylint --load-plugins=pylint_protobuf -E ${AGENT_DIR}

# Using a build arg to set the release tag, set a default for running docker build manually
ARG SRL_IXP_RELEASE="[custom build]"
ENV SRL_IXP_RELEASE=$SRL_IXP_RELEASE
