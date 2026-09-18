"""Single Core entry point; the registered release driver supplies physical-window budgets."""
import argparse
import os
from pathlib import Path
import sys


def reject_policy_environment(environ):
    removed = {'OPTIMIZER_RATIO', 'RATIO', 'OPRATIO'}
    for key in environ:
        canonical=key.upper()
        suffix=canonical.removeprefix('ADASELECT_').removeprefix('ADASEL_')
        if suffix in removed or canonical.startswith(('ADASEL_', 'ADASELECT_')):
            raise ValueError('unsupported policy environment input: '+key+'; use explicit policy JSON and QCP budget')


def main(argv=None):
    reject_policy_environment(os.environ)
    parser=argparse.ArgumentParser(description=__doc__)
    argv = list(sys.argv[1:] if argv is None else argv)
    retired = {'--optimizer_ratio', '--optimizer-ratio', '--opratio', '--ratio', '--max-evaluated-candidates'}
    for arg in argv:
        if arg.split('=', 1)[0] in retired:
            parser.error('removed policy input: '+arg.split('=', 1)[0]+'; supply an explicit QCP budget')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--deadline',type=float)
    parser.add_argument('mode',choices=['campaign','preflight','run'])
    parser.add_argument('workload',nargs='?')
    args=parser.parse_args(argv)
    from experiments.core_v2.release import campaign,preflight,run_one
    if args.mode=='campaign':campaign(args.output)
    elif args.deadline is None:parser.error('worker modes require the supervisor deadline')
    elif args.mode=='preflight':preflight(args.output,args.deadline)
    elif not args.workload:parser.error('run requires a registered workload')
    else:run_one(args.output,args.workload,args.deadline)


if __name__=='__main__':main()
