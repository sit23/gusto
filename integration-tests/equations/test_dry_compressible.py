"""
This runs a dry compressible example with a perturbation in a vertical slice,
and checks the example against a known good checkpointed answer.
"""

from os.path import join, abspath, dirname
from gusto import *
from gusto import thermodynamics as tde
from firedrake import (SpatialCoordinate, PeriodicIntervalMesh, exp,
                       sqrt, ExtrudedMesh, norm)


def run_dry_compressible(tmpdir):

    dt = 6.0
    tmax = 2*dt
    nlayers = 10  # horizontal layers
    ncols = 10  # number of columns
    Lx = 1000.0
    Lz = 1000.0
    m = PeriodicIntervalMesh(ncols, Lx)
    mesh = ExtrudedMesh(m, layers=nlayers, layer_height=Lz/nlayers)
    domain = Domain(mesh, dt, "CG", 1)

    parameters = CompressibleParameters()
    R_d = parameters.R_d
    g = parameters.g
    eqn = CompressibleEulerEquations(domain, parameters)

    output = OutputParameters(dirname=tmpdir+"/dry_compressible",
                              dumpfreq=2, chkptfreq=2)
    io = IO(domain, eqn, output=output)

    # Initial conditions
    rho0 = eqn.fields("rho")
    theta0 = eqn.fields("theta")

    # Approximate hydrostatic balance
    x, z = SpatialCoordinate(mesh)
    T = Constant(300.0)
    zH = R_d * T / g
    p = Constant(100000.0) * exp(-z / zH)
    theta0.interpolate(tde.theta(parameters, T, p))
    rho0.interpolate(p / (R_d * T))

    eqn.set_reference_profiles([('rho', rho0), ('theta', theta0)])

    # Add perturbation
    r = sqrt((x-Lx/2)**2 + (z-Lz/2)**2)
    theta_pert = 1.0*exp(-(r/(Lx/5))**2)
    theta0.interpolate(theta0 + theta_pert)

    # Set up transport schemes
    transported_fields = [ImplicitMidpoint(domain, "u"),
                          SSPRK3(domain, "rho"),
                          SSPRK3(domain, "theta")]

    # Set up linear solver for the timestepping scheme
    linear_solver = CompressibleSolver(eqn)

    # build time stepper
    stepper = SemiImplicitQuasiNewton(eqn, io, transported_fields,
                                      linear_solver=linear_solver)

    # Run
    stepper.run(t=0, tmax=tmax)

    # IO for checking checkpoints
    checkpoint_name = 'dry_compressible_chkpt'
    new_path = join(abspath(dirname(__file__)), '..', f'data/{checkpoint_name}')
    check_eqn = CompressibleEulerEquations(domain, parameters)
    check_output = OutputParameters(dirname=tmpdir+"/dry_compressible",
                                    checkpoint_pickup_filename=new_path)
    check_io = IO(domain, check_eqn, output=check_output)
    check_eqn.set_reference_profiles([])
    check_stepper = SemiImplicitQuasiNewton(check_eqn, check_io, [])
    check_stepper.run(t=0, tmax=0, pickup=True)

    return eqn, check_eqn


def test_dry_compressible(tmpdir):

    dirname = str(tmpdir)
    eqn, check_eqn = run_dry_compressible(dirname)

    for variable in ['u', 'rho', 'theta']:
        new_variable = eqn.fields(variable)
        check_variable = check_eqn.fields(variable)
        error = norm(new_variable - check_variable) / norm(check_variable)

        # Slack values chosen to be robust to different platforms
        assert error < 1e-10, f'Values for {variable} in ' + \
            'Dry Compressible test do not match KGO values'
