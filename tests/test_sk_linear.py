from gusto import *
from firedrake import Expression, \
    VectorFunctionSpace, PeriodicIntervalMesh, ExtrudedMesh, \
    exp, sin
import numpy as np


def setup_sk(dirname):
    nlayers = 10  # horizontal layers
    columns = 30  # number of columns
    L = 1.e5
    m = PeriodicIntervalMesh(columns, L)
    dt = 15.0

    # build volume mesh
    H = 1.0e4  # Height position of the model top
    mesh = ExtrudedMesh(m, layers=nlayers, layer_height=H/nlayers)

    # Space for initialising velocity
    W_VectorCG1 = VectorFunctionSpace(mesh, "CG", 1)
    W_CG1 = FunctionSpace(mesh, "CG", 1)

    # vertical coordinate and normal
    z = Function(W_CG1).interpolate(Expression("x[1]"))
    k = Function(W_VectorCG1).interpolate(Expression(("0.","1.")))

    fieldlist = ['u', 'rho', 'theta']
    timestepping = TimesteppingParameters(dt=dt)
    output = OutputParameters(dirname=dirname+"/sk_linear", dumplist=['u'], dumpfreq=150)
    diagnostics = Diagnostics(*fieldlist)
    parameters = CompressibleParameters()
    diagnostic_fields = [CourantNumber()]

    state = CompressibleState(mesh, vertical_degree=1, horizontal_degree=1,
                              family="CG",
                              z=z, k=k,
                              timestepping=timestepping,
                              output=output,
                              parameters=parameters,
                              diagnostics=diagnostics,
                              fieldlist=fieldlist,
                              diagnostic_fields=diagnostic_fields,
                              on_sphere=False)

    # Initial conditions
    u0, rho0, theta0 = Function(state.V[0]), Function(state.V[1]), Function(state.V[2])

    # Thermodynamic constants required for setting initial conditions
    # and reference profiles
    g = parameters.g
    N = parameters.N

    # N^2 = (g/theta)dtheta/dz => dtheta/dz = theta N^2g => theta=theta_0exp(N^2gz)
    Tsurf = 300.
    thetab = Tsurf*exp(N**2*z/g)

    theta_b = Function(state.V[2]).interpolate(thetab)
    rho_b = Function(state.V[1])
    compressible_hydrostatic_balance(state, theta_b, rho_b)

    W_DG1 = FunctionSpace(mesh, "DG", 1)
    x = Function(W_DG1).interpolate(Expression("x[0]"))
    a = 5.0e3
    deltaTheta = 1.0e-2
    theta_pert = deltaTheta*sin(np.pi*z/H)/(1 + (x - L/2)**2/a**2)
    theta0.interpolate(theta_b + theta_pert)
    rho0.assign(rho_b)

    state.initialise([u0, rho0, theta0])
    state.set_reference_profiles(rho_b, theta_b)
    state.output.meanfields = {'rho':state.rhobar, 'theta':state.thetabar}

    # Set up advection schemes
    advection_dict = {}
    advection_dict["u"] = NoAdvection(state)
    advection_dict["rho"] = LinearAdvection_V3(state, state.V[1], rho_b)
    advection_dict["theta"] = LinearAdvection_Vt(state, state.V[2], theta_b)

    # Set up linear solver
    schur_params = {'pc_type': 'fieldsplit',
                    'pc_fieldsplit_type': 'schur',
                    'ksp_type': 'gmres',
                    'ksp_monitor_true_residual': True,
                    'ksp_max_it': 100,
                    'ksp_gmres_restart': 50,
                    'pc_fieldsplit_schur_fact_type': 'FULL',
                    'pc_fieldsplit_schur_precondition': 'selfp',
                    'fieldsplit_0_ksp_type': 'richardson',
                    'fieldsplit_0_ksp_max_it': 5,
                    'fieldsplit_0_pc_type': 'bjacobi',
                    'fieldsplit_0_sub_pc_type': 'ilu',
                    'fieldsplit_1_ksp_type': 'richardson',
                    'fieldsplit_1_ksp_max_it': 5,
                    "fieldsplit_1_ksp_monitor_true_residual": True,
                    'fieldsplit_1_pc_type': 'gamg',
                    'fieldsplit_1_pc_gamg_sym_graph': True,
                    'fieldsplit_1_mg_levels_ksp_type': 'chebyshev',
                    'fieldsplit_1_mg_levels_ksp_chebyshev_estimate_eigenvalues': True,
                    'fieldsplit_1_mg_levels_ksp_chebyshev_estimate_eigenvalues_random': True,
                    'fieldsplit_1_mg_levels_ksp_max_it': 5,
                    'fieldsplit_1_mg_levels_pc_type': 'bjacobi',
                    'fieldsplit_1_mg_levels_sub_pc_type': 'ilu'}

    linear_solver = CompressibleSolver(state, params=schur_params)

    # Set up forcing
    compressible_forcing = CompressibleForcing(state, linear=True)

    # build time stepper
    stepper = Timestepper(state, advection_dict, linear_solver,
                          compressible_forcing)

    return stepper, dt


def run_sk_linear(dirname):

    stepper, tmax = setup_sk(dirname)
    stepper.run(t=0, tmax=tmax)


def test_sk(tmpdir):

    dirname = str(tmpdir)
    run_sk_linear(dirname)
