"""Strict Pydantic boundary DTOs for the three agent runtimes.

This package deliberately re-exports nothing. ``chorus.contracts.action`` is the only
public-safe contract, and a package-wide re-export would let the Action deployment artifact
pull the private Monitor and Investigator contracts in through one import of
``chorus.contracts``. Import the exact module you are allowed to see.
"""
