#!/usr/bin/env bash
# GPU 0
#source activate blaze
python /home/sshinoda/tf-ssl/vat_ladder/vat_ladder.py --id "GaussVatRC_seed-11" --seed 11 --vat_weight 1.0 --ent_weight 0.0 --which_gpu 0 --vat_rc --rc_weights 1000.0-10.0-0.10-0.10-0.10-0.10-0.10

python /home/sshinoda/tf-ssl/vat_ladder/vat_ladder.py --id "GaussVatRC_seed-111" --seed 111 --vat_weight 1.0 --ent_weight 0.0 --which_gpu 0 --vat_rc --rc_weights 1000.0-10.0-0.10-0.10-0.10-0.10-0.10 