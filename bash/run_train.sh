#!/bin/sh
#SBATCH -o ./slurm/%j-train.out # STDOUT

# python -m biom3d.train --config configs/20230413-unet_btcv.py
# python -m biom3d.train --log logs/20230501-153638-unet_default
python -m biom3d.train --config configs/20230509-181824-config_default.py