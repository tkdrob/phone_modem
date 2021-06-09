#!/usr/bin/env python
import setuptools

with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name='phone_modem',
    version='0.1.0',
    author='Robert Hillis',
    author_email='tkdrob4390@yahoo.com',
    description='An asynchronous modem implementation designed for Home Assistant for receiving caller id and call rejection.',
    long_description=long_description,
    long_description_content_type="text/markdown",
    url='https://github.com/tkdrob/phone_modem',
    packages=setuptools.find_packages(),
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires='>=3.6',
)
