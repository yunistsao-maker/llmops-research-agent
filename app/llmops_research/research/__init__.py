"""LLMOps portal research agent.

An Amazon Bedrock AgentCore Runtime agent (Strands) that reads public sources,
checks every claim against quoted evidence, and returns *proposals* for the
LLMOps portal data files.  It never writes to the portal itself.
"""

__version__ = "0.1.0"
