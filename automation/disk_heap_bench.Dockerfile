FROM ubuntu:24.04
RUN apt-get update -qq && apt-get install -y -qq python3 procps ca-certificates > /dev/null && rm -rf /var/lib/apt/lists/*
