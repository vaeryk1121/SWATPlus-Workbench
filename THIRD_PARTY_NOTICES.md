# Third-party components

SWATPlus Workbench application code is released by Xun Zhang under GPL-3.0-or-later.

The `Workbench/tools/swatcup_auto_upstream/swatcup_auto` modules originate in the same author's Swatcup-Auto project, revision `979cbdca725e365e79647ebfe39911033b2e4017`. This release includes those author-owned modules under this repository's GPL-3.0-or-later license. Its original third-party notice is preserved.

The Robit teaching inputs were exported locally using QSWAT+ 4.0.4 and Editor 4.0.2 from the official SWAT teaching data. The teaching-data source is https://swat.tamu.edu/software/quick-start/ . Source data retain their original authorship and rights. Example metadata does not establish observation accuracy.

The Windows package includes Python 3.9.1 and its standard-library/Tk runtime. The Python license is at `runtime/LICENSE.txt`; Tcl/Tk license texts are preserved under `runtime/tcl` and the corresponding library directories. Python source and release information are available at https://www.python.org/downloads/release/python-391/ .

The scientific Python dependencies and exact versions are recorded in `PYTHON_PACKAGES.json` in each binary distribution. Their original package license files and metadata remain under `runtime/Lib/site-packages`.

| Package | Upstream source |
| --- | --- |
| NumPy | https://github.com/numpy/numpy |
| SciPy | https://github.com/scipy/scipy |
| SPOTPY | https://github.com/thouska/spotpy |
| SALib | https://github.com/SALib/SALib |
| multiprocess / dill | https://github.com/uqfoundation |
| Matplotlib | https://github.com/matplotlib/matplotlib |
| pandas | https://github.com/pandas-dev/pandas |
| Pillow | https://github.com/python-pillow/Pillow |

Other transitive dependencies retain their accompanying license files. Package source links are recorded in their `.dist-info/METADATA` files.

QGIS, QSWAT+, SWAT+ Editor/model engine, TauDEM, Microsoft MPI, Toolbox and classic SUFI-2 executables are external installations. The release discovers and invokes compatible installed programs. Their project names and licenses remain with the respective authors.
