"""AgentCore Runtime adapters.

One narrow invoker carries bytes to a named runtime endpoint and back. Everything above it is
typed against the agent contracts, so the application never learns which AWS service answered.
"""
