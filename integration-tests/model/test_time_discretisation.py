from firedrake import norm
from gusto import *
import pytest


def run(timestepper, tmax, f_end):
    timestepper.run(0, tmax)
    return norm(timestepper.fields("f") - f_end) / norm(f_end)


@pytest.mark.parametrize("scheme", ["ssprk", "implicit_midpoint",
                                    "RK4", "Heun", "BDF2", "TR_BDF2", "AB2", "AB3", "Leapfrog", "AM2"])
def test_time_discretisation(tmpdir, scheme, tracer_setup):
    if (scheme == "AB2" or scheme == "AB3"):
        # Tighter stability constraints
        geometry = "sphere"
        setup = tracer_setup(tmpdir, geometry, small_dt=True)
        domain = setup.domain
        V = domain.spaces("DG")
        eqn = AdvectionEquation(domain, V, "f")
    else:
        geometry = "sphere"
        setup = tracer_setup(tmpdir, geometry)
        domain = setup.domain
        V = domain.spaces("DG")
        eqn = AdvectionEquation(domain, V, "f")

    if scheme == "ssprk":
        transport_scheme = SSPRK3(domain)
    elif scheme == "implicit_midpoint":
        transport_scheme = ImplicitMidpoint(domain)
    elif scheme == "RK4":
        transport_scheme = RK4(domain)
    elif scheme == "Heun":
        transport_scheme = Heun(domain)
    elif scheme == "BDF2":
        transport_scheme = BDF2(domain)
    elif scheme == "TR_BDF2":
        transport_scheme = TR_BDF2(domain, gamma=0.5)
    elif scheme == "Leapfrog":
        # Leapfrog unstable with DG
        Vf = domain.spaces("CG", "CG", 1)
        eqn = AdvectionEquation(domain, Vf, "f")
        transport_scheme = Leapfrog(domain)
    elif scheme == "AB2":
        transport_scheme = AB2(domain)
    elif scheme == "AB3":
        transport_scheme = AB3(domain)
    elif scheme == "AM2":
        transport_scheme = AM2(domain)

    timestepper = PrescribedTransport(eqn, transport_scheme, setup.io)

    # Initial conditions
    timestepper.fields("f").interpolate(setup.f_init)
    timestepper.fields("u").project(setup.uexpr)
    assert run(timestepper, setup.tmax, setup.f_end) < setup.tol
