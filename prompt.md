these are different script for downloading pkgs from pypi or mirrors of pypi
inspect them and use them to write one final script to download pkg(s) with following features:
- backends: httpx(default),pycurl,requests
- script must accept multiple pkg names with version pinned (like requests==0.23.5 pycurl==2.5.9) as input
  also must have ability to read pkg names (with possible versions) from input file with -i flag
- must save downloaded files to current dir (no subdir creation needed)
- must download latest .tar.gz ( or .zip , .tar.bz2) of pkgs, if not available, must download linux compatible .whl with tag of current python (if available) ->  this is for none-pure python pkgs
- must show a progress with rich if download took more than 15 secs
- must support parallel download
- above features are base ones you can add other features from scripts too
save result in output folder plus pytest based tests & example demo usages
result code must be fully type annotated & documented
