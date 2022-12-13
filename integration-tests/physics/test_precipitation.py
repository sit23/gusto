"""
This tests the precipitation scheme. The test uses a cloud of rain that falls at
its terminal velocity, which is prescribed in the fallout method in physics.py.
The test passes if there is no rain remaining at the end of the test.
"""

from os import path
from gusto import *
from firedrake import (PeriodicIntervalMesh, SpatialCoordinate,
                       ExtrudedMesh, sqrt, conditional, cos, pi)
from netCDF4 import Dataset


def setup_fallout(dirname):

    # declare grid shape, with length L and height H
    dt = 0.1
    L = 10.
    H = 10.
    nlayers = 10
    ncolumns = 10

    # make mesh
    m = PeriodicIntervalMesh(ncolumns, L)
    mesh = ExtrudedMesh(m, layers=nlayers, layer_height=(H / nlayers))
    domain = Domain(mesh, dt, "CG", 1)
    x = SpatialCoordinate(mesh)

    Vrho = domain.spaces("DG1_equispaced")
    active_tracers = [Rain(space='DG1_equispaced')]
    eqn = ForcedAdvectionEquation(domain, Vrho, "rho", active_tracers=active_tracers)

    output = OutputParameters(dirname=dirname+"/fallout", dumpfreq=10, dumplist=['rain'])
    diagnostic_fields = [Precipitation()]
    io = IO(domain, eqn, output=output, diagnostic_fields=diagnostic_fields)

    scheme = ForwardEuler(domain)
    eqn.fields("rho").assign(1.)

    physics_schemes = [(Fallout(eqn, 'rain', domain), SSPRK3(domain, 'rain'))]
    rain0 = eqn.fields("rain")

    # set up rain
    xc = L / 2
    zc = H / 2
    rc = H / 4
    r = sqrt((x[0] - xc) ** 2 + (x[1] - zc) ** 2)
    rain_expr = conditional(r > rc, 0., 1e-3 * (cos(pi * r / (rc * 2))) ** 2)

    rain0.interpolate(rain_expr)

    # build time stepper
    stepper = PrescribedTransport(eqn, scheme, io,
                                  physics_schemes=physics_schemes)

    return stepper, 10.0


def run_fallout(dirname):

    stepper, tmax = setup_fallout(dirname)
    stepper.run(t=0, tmax=tmax)


def test_fallout_setup(tmpdir):

    dirname = str(tmpdir)
    run_fallout(dirname)
    filename = path.join(dirname, "fallout/diagnostics.nc")
    data = Dataset(filename, "r")

    rain = data.groups["rain"]
    final_rain = rain.variables["total"][-1]
    final_rms_rain = rain.variables["rms"][-1]

    assert abs(final_rain) < 1e-4
    assert abs(final_rms_rain) < 1e-4
