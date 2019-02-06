from abc import ABCMeta, abstractmethod
from enum import Enum
from firedrake import (Function, TestFunction, TrialFunction, FacetNormal,
                       dx, dot, grad, div, jump, avg, dS, dS_v, dS_h, inner,
                       ds, ds_v, ds_t, ds_b,
                       outer, sign, cross, CellNormal, Constant,
                       curl, BrokenElement, FunctionSpace)
from gusto.configuration import logger, DEBUG, SUPGOptions


__all__ = ["LinearAdvection", "AdvectionEquation", "EmbeddedDGAdvection", "SUPGAdvection", "VectorInvariant", "EulerPoincare", "IntegrateByParts"]


class IntegrateByParts(Enum):
    NEVER = 0
    ONCE = 1
    TWICE = 2


def is_cg(V):
    # find out if we are CG
    nvertex = V.ufl_domain().ufl_cell().num_vertices()
    entity_dofs = V.finat_element.entity_dofs()
    # If there are as many dofs on vertices as there are vertices,
    # assume a continuous space.
    try:
        return sum(map(len, entity_dofs[0].values())) == nvertex
    except KeyError:
        return sum(map(len, entity_dofs[(0, 0)].values())) == nvertex


def surface_measures(V, direction=None):
    if is_cg(V):
        return None, None
    else:
        if V.extruded:
            return (dS_h + dS_v), (ds_b + ds_t + ds_v)
        else:
            return dS, ds


class TransportEquation(object, metaclass=ABCMeta):
    """
    Base class for transport equations in Gusto.

    The equation is assumed to be in the form:

    q_t + L(q) = 0

    where q is the (scalar or vector) field to be solved for.

    :arg state: :class:`.State` object.
    :arg V: :class:`.FunctionSpace object. The function space that q lives in.
    :arg ibp: string, stands for 'integrate by parts' and can take the value
              None, "once" or "twice". Defaults to "once".
    :arg solver_params: (optional) dictionary of solver parameters to pass to the
                        linear solver.
    """

    def __init__(self, state, V, *, ibp=IntegrateByParts.ONCE, solver_params=None):
        self.state = state
        self.V = V
        self.ibp = ibp

        # set up functions required for forms
        self.ubar = Function(state.spaces("HDiv"))
        self.test = TestFunction(V)
        self.trial = TrialFunction(V)

        self.dS, self.ds = surface_measures(V)

        if solver_params:
            self.solver_parameters = solver_params

        # default solver options
        else:
            self.solver_parameters = {'ksp_type': 'cg',
                                      'pc_type': 'bjacobi',
                                      'sub_pc_type': 'ilu'}
        if logger.isEnabledFor(DEBUG):
            self.solver_parameters["ksp_monitor_true_residual"] = True

    def mass_term(self, q):
        return inner(self.test, q)*dx

    @abstractmethod
    def advection_term(self):
        pass


class LinearAdvection(TransportEquation):
    """
    Class for linear transport equation.

    :arg state: :class:`.State` object.
    :arg V: :class:`.FunctionSpace object. The function space that q lives in.
    :arg qbar: The reference function that the equation has been linearised
               about. It is assumed that the reference velocity is zero and
               the ubar below is the nonlinear advecting velocity
               0.5*(u'^(n+1) + u'(n)))
    :arg ibp: string, stands for 'integrate by parts' and can take the value
              None, "once" or "twice". Defaults to "once".
    :arg equation_form: (optional) string, can take the values 'continuity',
                        which means the equation is in continuity form
                        L(q) = div(u'*qbar), or 'advective', which means the
                        equation is in advective form L(q) = u' dot grad(qbar).
                        Default is "advective"
    :arg solver_params: (optional) dictionary of solver parameters to pass to the
                        linear solver.
    """

    def __init__(self, state, V, qbar, ibp=IntegrateByParts.NEVER, equation_form="advective", solver_params=None):
        super().__init__(state=state, V=V, ibp=ibp, solver_params=solver_params)
        if equation_form == "advective" or equation_form == "continuity":
            self.continuity = (equation_form == "continuity")
        else:
            raise ValueError("equation_form must be either 'advective' or 'continuity', not %s" % equation_form)

        self.qbar = qbar

        # currently only used with the following option combinations:
        if self.continuity and ibp != IntegrateByParts.ONCE:
            raise NotImplementedError("If we are solving a linear continuity equation, we integrate by parts once")
        if not self.continuity and ibp != IntegrateByParts.NEVER:
            raise NotImplementedError("If we are solving a linear advection equation, we do not integrate by parts.")

        # default solver options
        self.solver_parameters = {'ksp_type': 'cg',
                                  'pc_type': 'bjacobi',
                                  'sub_pc_type': 'ilu'}

    def advection_term(self, q):

        if self.continuity:
            n = FacetNormal(self.state.mesh)
            L = (-dot(grad(self.test), self.ubar)*self.qbar*dx
                 + jump(self.ubar*self.test, n)*avg(self.qbar)*self.dS)
        else:
            L = self.test*dot(self.ubar, self.state.k)*dot(self.state.k, grad(self.qbar))*dx
        return L


class AdvectionEquation(TransportEquation):
    """
    Class for discretisation of the transport equation.

    :arg state: :class:`.State` object.
    :arg V: :class:`.FunctionSpace object. The function space that q lives in.
    :arg ibp: string, stands for 'integrate by parts' and can take the value
              None, "once" or "twice". Defaults to "once".
    :arg equation_form: (optional) string, can take the values 'continuity',
                        which means the equation is in continuity form
                        L(q) = div(u*q), or 'advective', which means the
                        equation is in advective form L(q) = u dot grad(q).
                        Default is "advective"
    :arg vector_manifold: Boolean. If true adds extra terms that are needed for
    advecting vector equations on manifolds.
    :arg solver_params: (optional) dictionary of solver parameters to pass to the
                        linear solver.
    :arg outflow: Boolean specifying whether advected quantity can be advected out
                  of domain.
    """
    def __init__(self, state, V, *, ibp=IntegrateByParts.ONCE, equation_form="advective",
                 vector_manifold=False, solver_params=None, outflow=False):
        super().__init__(state=state, V=V, ibp=ibp, solver_params=solver_params)
        if equation_form == "advective" or equation_form == "continuity":
            self.continuity = (equation_form == "continuity")
        else:
            raise ValueError("equation_form must be either 'advective' or 'continuity'")
        self.vector_manifold = vector_manifold
        self.outflow = outflow
        if outflow and ibp == IntegrateByParts.NEVER:
            raise ValueError("outflow is True and ibp is None are incompatible options")

    def advection_term(self, q):

        if self.continuity:
            if self.ibp == IntegrateByParts.ONCE:
                L = -inner(grad(self.test), outer(q, self.ubar))*dx
            else:
                L = inner(self.test, div(outer(q, self.ubar)))*dx
        else:
            if self.ibp == IntegrateByParts.ONCE:
                L = -inner(div(outer(self.test, self.ubar)), q)*dx
            else:
                L = inner(outer(self.test, self.ubar), grad(q))*dx

        if self.dS is not None and self.ibp != IntegrateByParts.NEVER:
            n = FacetNormal(self.state.mesh)
            un = 0.5*(dot(self.ubar, n) + abs(dot(self.ubar, n)))

            L += dot(jump(self.test), (un('+')*q('+')
                                       - un('-')*q('-')))*self.dS

            if self.ibp == IntegrateByParts.TWICE:
                L -= (inner(self.test('+'),
                            dot(self.ubar('+'), n('+'))*q('+'))
                      + inner(self.test('-'),
                              dot(self.ubar('-'), n('-'))*q('-')))*self.dS

        if self.outflow:
            n = FacetNormal(self.state.mesh)
            un = 0.5*(dot(self.ubar, n) + abs(dot(self.ubar, n)))
            L += self.test*un*q*self.ds

        if self.vector_manifold:
            n = FacetNormal(self.state.mesh)
            w = self.test
            dS = self.dS
            u = q
            L += un('+')*inner(w('-'), n('+')+n('-'))*inner(u('+'), n('+'))*dS
            L += un('-')*inner(w('+'), n('+')+n('-'))*inner(u('-'), n('-'))*dS
        return L


class EmbeddedDGAdvection(AdvectionEquation):
    """
    Class for the transport equation, using an embedded DG advection scheme.

    :arg state: :class:`.State` object.
    :arg V: :class:`.FunctionSpace object. The function space that q lives in.
    :arg ibp: (optional) string, stands for 'integrate by parts' and can take
              the value None, "once" or "twice". Defaults to "once".
    :arg equation_form: (optional) string, can take the values 'continuity',
                        which means the equation is in continuity form
                        L(q) = div(u*q), or 'advective', which means the
                        equation is in advective form L(q) = u dot grad(q).
                        Default is "advective"
    :arg vector_manifold: Boolean. If true adds extra terms that are needed for
    advecting vector equations on manifolds.
    :arg solver_params: (optional) dictionary of solver parameters to pass to the
                        linear solver.
    :arg outflow: Boolean specifying whether advected quantity can be advected out of domain.
    :arg options: an instance of the AdvectionOptions class specifying which options to use
                  with the embedded DG scheme.
    """

    def __init__(self, state, V, ibp=IntegrateByParts.ONCE,
                 equation_form="advective",
                 vector_manifold=False,
                 solver_params=None, outflow=False, options=None):

        if options is None:
            raise ValueError("Must provide an instance of the AdvectionOptions class")
        else:
            self.options = options
        if options.name == "embedded_dg" and options.embedding_space is None:
            V_elt = BrokenElement(V.ufl_element())
            options.embedding_space = FunctionSpace(state.mesh, V_elt)

        super().__init__(state=state,
                         V=options.embedding_space,
                         ibp=ibp,
                         equation_form=equation_form,
                         vector_manifold=vector_manifold,
                         solver_params=solver_params,
                         outflow=outflow)


class SUPGAdvection(AdvectionEquation):
    """
    Class for the transport equation.

    :arg state: :class:`.State` object.
    :arg V: :class:`.FunctionSpace object. The function space that q lives in.
    :arg ibp: (optional) string, stands for 'integrate by parts' and can
              take the value None, "once" or "twice". Defaults to "twice"
              since we commonly use this scheme for parially continuous
              spaces, in which case we don't want to take a derivative of
              the test function. If using for a fully continuous space, we
              don't integrate by parts at all (so you can set ibp=None).
    :arg equation_form: (optional) string, can take the values 'continuity',
                        which means the equation is in continuity form
                        L(q) = div(u*q), or 'advective', which means the
                        equation is in advective form L(q) = u dot grad(q).
                        Default is "advective"
    :arg supg_params: (optional) dictionary of parameters for the SUPG method.
                      Can contain:
                      'ax', 'ay', 'az', which specify the coefficients in
                      the x, y, z directions respectively
                      'dg_direction', which can be 'horizontal' or 'vertical',
                      and specifies the direction in which the function space
                      is discontinuous so that we can apply DG upwinding in
                      this direction.
                      Appropriate defaults are provided for these parameters,
                      in particular, the space is assumed to be continuous.
    :arg solver_params: (optional) dictionary of solver parameters to pass to the
                        linear solver.
    :arg outflow: Boolean specifying whether advected quantity can be advected out
                  of domain.
    """
    def __init__(self, state, V, ibp=IntegrateByParts.TWICE, equation_form="advective", supg_params=None, solver_params=None, outflow=False):

        if not solver_params:
            # SUPG method leads to asymmetric matrix (since the test function
            # is effectively modified), so don't use CG
            solver_params = {'ksp_type': 'gmres',
                             'pc_type': 'bjacobi',
                             'sub_pc_type': 'ilu'}

        super().__init__(state=state, V=V, ibp=ibp,
                         equation_form=equation_form,
                         solver_params=solver_params,
                         outflow=outflow)

        # if using SUPG we either integrate by parts twice, or not at all
        if ibp == IntegrateByParts.ONCE:
            raise ValueError("if using SUPG we don't integrate by parts once")
        if ibp == IntegrateByParts.NEVER and not is_cg(V):
            raise ValueError("are you very sure you don't need surface terms?")

        if supg_params is None:
            supg_params = SUPGOptions()
        # set default SUPG parameters
        dt = state.timestepping.dt
        dim = state.mesh.topological_dimension()
        if supg_params.tau is not None:
            tau = supg_params.tau
            assert tau.ufl_shape == (dim, dim)
        else:
            vals = [supg_params.default*dt]*dim
            for component, value in supg_params.tau_components:
                vals[state.components.component] = value
            tau = Constant(tuple([
                tuple(
                    [vals[j] if i == j else 0. for i, v in enumerate(vals)]
                ) for j in range(dim)])
            )
        dtest = dot(dot(self.ubar, tau), grad(self.test))
        self.test += dtest


class VectorInvariant(TransportEquation):
    """
    Class defining the vector invariant form of the vector advection equation.

    :arg state: :class:`.State` object.
    :arg V: Function space
    :arg ibp: (optional) string, stands for 'integrate by parts' and can
              take the value None, "once" or "twice". Defaults to "once".
    :arg solver_params: (optional) dictionary of solver parameters to pass to the
                        linear solver.
    """
    def __init__(self, state, V, *, ibp=IntegrateByParts.ONCE,
                 solver_params=None):
        super().__init__(state=state, V=V, ibp=ibp,
                         solver_params=solver_params)

        if state.mesh.topological_dimension() == 3 and ibp == IntegrateByParts.TWICE:
            raise NotImplementedError("ibp=twice is not implemented for 3d problems")

    def advection_term(self, q):

        n = FacetNormal(self.state.mesh)
        Upwind = 0.5*(sign(dot(self.ubar, n))+1)

        if self.state.mesh.topological_dimension() == 3:
            # <w,curl(u) cross ubar + grad( u.ubar)>
            # =<curl(u),ubar cross w> - <div(w), u.ubar>
            # =<u,curl(ubar cross w)> -
            #      <<u_upwind, [[n cross(ubar cross w)cross]]>>

            both = lambda u: 2*avg(u)

            L = (
                inner(q, curl(cross(self.ubar, self.test)))*dx
                - inner(both(Upwind*q),
                        both(cross(n, cross(self.ubar, self.test))))*self.dS
            )

        else:

            perp = self.state.perp
            if self.state.on_sphere:
                outward_normals = CellNormal(self.state.mesh)
                perp_u_upwind = lambda q: Upwind('+')*cross(outward_normals('+'), q('+')) + Upwind('-')*cross(outward_normals('-'), q('-'))
            else:
                perp_u_upwind = lambda q: Upwind('+')*perp(q('+')) + Upwind('-')*perp(q('-'))
            gradperp = lambda u: perp(grad(u))

            if self.ibp == IntegrateByParts.ONCE:
                L = (
                    -inner(gradperp(inner(self.test, perp(self.ubar))), q)*dx
                    - inner(jump(inner(self.test, perp(self.ubar)), n),
                            perp_u_upwind(q))*self.dS
                )
            else:
                L = (
                    (-inner(self.test, div(perp(q))*perp(self.ubar)))*dx
                    - inner(jump(inner(self.test, perp(self.ubar)), n),
                            perp_u_upwind(q))*self.dS
                    + jump(inner(self.test,
                                 perp(self.ubar))*perp(q), n)*self.dS
                )

        L -= 0.5*div(self.test)*inner(q, self.ubar)*dx

        return L


class EulerPoincare(VectorInvariant):
    """
    Class defining the Euler-Poincare form of the vector advection equation.

    :arg state: :class:`.State` object.
    :arg V: Function space
    :arg ibp: string, stands for 'integrate by parts' and can take the value
              None, "once" or "twice". Defaults to "once".
    :arg solver_params: (optional) dictionary of solver parameters to pass to the
                        linear solver.
    """

    def advection_term(self, q):
        L = super().advection_term(q)
        L -= 0.5*div(self.test)*inner(q, self.ubar)*dx
        return L
