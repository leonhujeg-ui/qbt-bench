"""场景文件目录：qdexsim/scenarios/<dialect>__<template>.json，由 scenarios/gen.py 生成（判据：生成结果 == 磁盘文件）。"""
import glob
import os

from qdexsim import common

DIR = os.path.dirname(os.path.abspath(__file__))


def paths():
    return sorted(glob.glob(os.path.join(DIR, "*.json")))


def load_all(dialect=None, template=None):
    out = []
    for p in paths():
        scn = common.read_json(p)
        if dialect and scn["dialect"] != dialect:
            continue
        if template and scn["template"] != template:
            continue
        out.append(scn)
    return out


def load(name):
    for scn in load_all():
        if scn["name"] == name:
            return scn
    raise KeyError(name)
