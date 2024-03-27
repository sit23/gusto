u"""
Objects for discretising time derivatives using Spectral Deferred Correction
Methods.

SDC objects discretise ∂y/∂t = F(y), for variable y, time t and
operator F.

Written in Picard integral form this equation is
y(t) = y_0 + int[t_0,t] F(y(s)) ds

Using some quadrature rule, we can evaluate y on a temporal quadrature node as
y_m = y_0 + sum[j=1,M] q_mj*F(y_j)
where q_mj can be found by integrating Lagrange polynomials. This is similar to
how Runge-Kutta methods are formed.

In matrix form this equation is:
(I - dt*Q*F)(y)=y_0

Computing y by Picard iteration through k we get:
y^(k+1)=y^k + (y_0 - (I - dt*Q*F))(y^k)

Finally, to get our SDC method we precondition this system, using some approximation
of Q Q_delta:
(I - dt*Q_deltaF)(y^(k+1)) = y_0 + dt*(Q - Q-delta)F(y^k)

Node-wise from previous quadrature node (using Forward Euler Q_delta) this calculation is:
y_m^(k+1) = y_(m-1)^(k+1) + dtau_m*(F(y_(m-1)^(k+1)) - F(y_(m-1)^k))
            + sum(j=1,M) s_mj*F(y^k)
where s_mj = q_mj - q_(m-1)j for entires q_ik in Q.

Key choices in our SDC method are: 
- Choice of quadrature node type (e.g. gauss-lobatto)
- Number of quadrature nodes
- Number of iterations - each iteration increases the order of accuracy up to
  the order of the underlying quadrature
- Choice of Q_delta (e.g. ForwardEuler, Backward Euler, LU-trick)
- How to get initial solution on quadrature nodes
"""

from abc import ABCMeta, abstractmethod, abstractproperty
import numpy as np
from firedrake import (
    Function, NonlinearVariationalProblem,
    NonlinearVariationalSolver
)
from firedrake.fml import (
    replace_subject, all_terms, drop
)
from firedrake.utils import cached_property

from gusto.labels import (time_derivative, implicit, explicit)
from gusto.wrappers import *
from gusto.time_discretisation import *
import scipy
from scipy.special import legendre


__all__ = ["BE_SDC", "FE_SDC", "IMEX_SDC"]

class SDC(object, metaclass=ABCMeta):
    """Base class for Spectral Deferred Correction schemes."""

    def __init__(self, base_scheme, domain, M, maxk, quadrature, field_name=None, 
                 linear_solver_parameters=None, nonlinear_solver_parameters=None, final_update=True,
                 limiter=None, options=None):
            """
            Initialise SDC object
            Args:
                base_scheme (:class:`TimeDiscretisation`): Base time stepping scheme to get first guess of solution on 
                    quadrature nodes.
                domain (:class:`Domain`): the model's domain object, containing the
                    mesh and the compatible function spaces.
                M (int): Number of quadrature nodes to compute spectral integration over
                maxk (int): Max number of correction interations
                quadrature (str): Type of quadrature to be used. Options are gauss-legendre,
                    gauss-radau and gauss-lobotto.
                field_name (str, optional): name of the field to be evolved.
                    Defaults to None.
                linear_solver_parameters (dict, optional): dictionary of parameters to
                    pass to the underlying linear solver. Defaults to None.
                nonlinear_solver_parameters (dict, optional): dictionary of parameters to
                    pass to the underlying nonlinear solver. Defaults to None.
                final_update (bool, optional): Whether to compute final update, or just take last
                    quadrature value. Defaults to True
                limiter (:class:`Limiter` object, optional): a limiter to apply to
                the evolving field to enforce monotonicity. Defaults to None.
                options (:class:`AdvectionOptions`, optional): an object containing
                    options to either be passed to the spatial discretisation, or
                    to control the "wrapper" methods, such as Embedded DG or a
                    recovery method. Defaults to None.
            """
            self.base = base_scheme
            self.field_name = field_name
            self.domain = domain
            self.dt_coarse = domain.dt
            self.M = M
            self.maxk = maxk
            self.final_update = final_update
            self.options = options
            self.limiter = limiter
            self.courant_max = None

            if options is not None:
                self.wrapper_name = options.name
                if self.wrapper_name == "mixed_options":
                    self.wrapper = MixedFSWrapper()

                    for field, suboption in options.suboptions.items():
                        if suboption.name == 'embedded_dg':
                            self.wrapper.subwrappers.update({field: EmbeddedDGWrapper(self, suboption)})
                        elif suboption.name == "recovered":
                            self.wrapper.subwrappers.update({field: RecoveryWrapper(self, suboption)})
                        elif suboption.name == "supg":
                            raise RuntimeError(
                                'Time discretisation: suboption SUPG is currently not implemented within MixedOptions')
                        else:
                            raise RuntimeError(
                                f'Time discretisation: suboption wrapper {wrapper_name} not implemented')
                elif self.wrapper_name == "embedded_dg":
                    self.wrapper = EmbeddedDGWrapper(self, options)
                elif self.wrapper_name == "recovered":
                    self.wrapper = RecoveryWrapper(self, options)
                elif self.wrapper_name == "supg":
                    self.wrapper = SUPGWrapper(self, options)
                else:
                    raise RuntimeError(
                        f'Time discretisation: wrapper {self.wrapper_name} not implemented')
            else:
                self.wrapper = None
                self.wrapper_name = None

            # get default linear and nonlinear solver options if none passed in
            if linear_solver_parameters is None:
                self.linear_solver_parameters = {'snes_type': 'ksponly',
                                                'ksp_type': 'cg',
                                                'pc_type': 'bjacobi',
                                                'sub_pc_type': 'ilu'}
                self.linear_solver_parameters = linear_solver_parameters

            if nonlinear_solver_parameters is None:
                self.nonlinear_solver_parameters = {'snes_type': 'newtonls',
                                                    'ksp_type': 'gmres',
                                                    'pc_type': 'bjacobi',
                                                    'sub_pc_type': 'ilu'}
            else:
                self.nonlinear_solver_parameters = nonlinear_solver_parameters

            # Set up quadrature nodes over [0.dt] and create
            # the various integration matrices
            self.create_nodes(self.dt_coarse, quadrature)
            self.Qmatrix()
            self.Smatrix()
            self.Qfinal()
            self.dtau = np.diff(np.append(0, self.nodes))

    def setup(self, equation, apply_bcs=True, *active_labels):
        """
        Set up the SDC time discretisation based on the equation.

        Args:
            equation (:class:`PrognosticEquation`): the model's equation.
            *active_labels (:class:`Label`): labels indicating which terms of
                the equation to include.
        """
        # Inherit from base time discretisation
        self.base.setup(equation, apply_bcs, *active_labels)
        self.residual = self.base.residual
        self.evaluate_source = self.base.evalaute_source
        self.physics_names = self.base.physics_names
        self.wrapper = self.base.wrapper
        self.fs = self.base.fs

        # set up SDC variables
        if self.field_name is not None and hasattr(equation, "field_names"):
            self.idx = equation.field_names.index(self.field_name)
            W = equation.spaces[self.idx]
        else:
            self.field_name = equation.field_name
            W = equation.function_space
            self.idx = None
        self.W = W
        self.Unodes = [Function(W) for _ in range(self.M+1)]
        self.Unodes1 = [Function(W) for _ in range(self.M+1)]
        self.fUnodes = [Function(W) for _ in range(self.M+1)]
        self.quad = [Function(W) for _ in range(self.M+1)]
        self.U_SDC = Function(W)
        self.U0 = Function(W)
        self.U01 = Function(W)
        self.Un = Function(W)
        self.Q_ = Function(W)
        self.quad_final = Function(W)
        self.U_fin = Function(W)
        self.Urhs = Function(W)
        self.Uin = Function(W)

        # Make boundary conditions
        self.bcs = self.base.bcs

    @property
    def nlevels(self):
        return 1

    @property
    def res_rhs(self):
        """Set up the residual for the calculation of F(Y)."""
        a = self.residual.label_map(lambda t: t.has_label(time_derivative),
                                    replace_subject(self.Urhs, old_idx=self.idx),
                                    drop)
        # F(y)
        L = self.residual.label_map(lambda t: t.has_label(time_derivative),
                                    drop,
                                    replace_subject(self.Uin, old_idx=self.idx))
        residual_rhs = a - L
        return residual_rhs.form
    
    @property
    def res_SDC(self):
        """Set up the residual for the SDC solve."""
        F = self.residual.label_map(lambda t: t.has_label(time_derivative),
                                    map_if_false=lambda t: self.dt_imp*t)

        # y_m^(k+1)
        a = F.label_map(lambda t: t.has_label(time_derivative),
                        replace_subject(self.U_SDC, old_idx=self.idx),
                        drop)

        # y_(m-1)^(k+1) + dt*F(y_(m-1)^(k+1))
        F_exp = F.label_map(all_terms, replace_subject(self.Un, old_idx=self.idx))
        F_exp = F_exp.label_map(lambda t: t.has_label(time_derivative),
                                lambda t: -1*t)

        # dt*F(y_(m-1)^k)
        F0 = F.label_map(lambda t: t.has_label(time_derivative),
                         drop,
                         replace_subject(self.U0, old_idx=self.idx))
        F0 = F0.label_map(all_terms,
                          lambda t: -1*t)

        # sum(j=1,M) s_mj*F(y_m^k
        Q = self.residual.label_map(lambda t: t.has_label(time_derivative),
                                    replace_subject(self.Q_, old_idx=self.idx),
                                    drop)

        residual_SDC = a + F_exp + F0 + Q
        return residual_SDC.form

    @property
    def res_fin(self):
        """Set up the residual for final solve."""
        # y^(n+1)
        a = self.residual.label_map(lambda t: t.has_label(time_derivative),
                        replace_subject(self.U_fin, old_idx=self.idx),
                        drop)
        # y^n
        F_exp = self.residual.label_map(lambda t: t.has_label(time_derivative),
                            replace_subject(self.Un, old_idx=self.idx),
                            drop)
        F_exp = F_exp.label_map(lambda t: t.has_label(time_derivative),
                                lambda t: -1*t)

        # sum(j=1,M) q_j*F(y_j)
        Q = self.residual.label_map(lambda t: t.has_label(time_derivative),
                                    replace_subject(self.quad_final, old_idx=self.idx),
                                    drop)

        residual_final = a + F_exp + Q
        return residual_final.form
    
    @cached_property
    def solver_fin(self):
        """Set up the problem and the solver for final update."""
        # setup linear solver using final residual defined in derived class
        prob_fin = NonlinearVariationalProblem(self.res_fin, self.U_fin, bcs=self.bcs)
        solver_name = self.field_name+self.__class__.__name__+"_final"
        return NonlinearVariationalSolver(prob_fin, solver_parameters=self.linear_solver_parameters,
                                          options_prefix=solver_name)
    
    @cached_property
    def solver_SDC(self):
        """Set up the problem and the solver for SDC solve."""
        # setup linear solver using SDC residual defined in derived class
        prob_SDC = NonlinearVariationalProblem(self.res_SDC, self.U_SDC, bcs=self.bcs)
        solver_name = self.field_name+self.__class__.__name__+"_SDC"
        return NonlinearVariationalSolver(prob_SDC, solver_parameters=self.nonlinear_solver_parameters,
                                          options_prefix=solver_name)
    
    @cached_property
    def solver_rhs(self):
        """Set up the problem and the solver for mass matrix inversion."""
        # setup linear solver using rhs residual defined in derived class
        prob_rhs = NonlinearVariationalProblem(self.res_rhs, self.Urhs, bcs=self.bcs)
        solver_name = self.field_name+self.__class__.__name__+"_rhs"
        return NonlinearVariationalSolver(prob_rhs, solver_parameters=self.linear_solver_parameters,
                                          options_prefix=solver_name)

    def create_nodes(self, b, quadrature, A=-1, B=1):
        """
        Create quadrature nodes over range [0,b]
        Args:
            b (real): End of quadrature domain (start is a=0)
            quadrature (str): Type of quadrature to be used. Options are gauss-legendre,
                gauss-radau and gauss-lobotto.
        """
        M = self.M
        a = 0.
        nodes = np.zeros(M)
        if quadrature == "gauss-radau":
            # nodes and weights for gauss - radau IIA quadrature
            # See Abramowitz & Stegun p 888
            nodes[0] = A
            p = np.poly1d([1, 1])
            pn = legendre(M)
            pn1 = legendre(M-1)
            poly, remainder = (pn + pn1)/p  # [1] returns remainder from polynomial division
            nodes[1:] = np.sort(poly.roots)
        elif quadrature == "gauss-lobatto":
            # nodes and weights for gauss - lobatto quadrature
            pn = legendre(M-1)
            pn_d=pn.deriv()
            nodes[0] = A
            nodes[-1]= B
            nodes[1:-1] = np.sort(pn_d.roots)
        elif quadrature == "gauss-legendre":
            # nodes and weights for gauss - legendre quadrature
            pn = legendre(M)
            nodes = np.sort(pn.roots)

        # rescale to be between [a,b] instead of [A,B]
        nodes = ((b - a) * nodes + a * B - b * A) / (B - A)
        self.nodes = ((b + a) - nodes)[::-1]  # reverse nodes

    def NewtonVM(self, t):
        """
        Create Newton Vandermode Matrix. Entries are in the lower
        triangle. Polynomial can be created with
        scipy.linalg.solve_triangular(NewtonVM(t),y,lower=True) where y
        contains the points the polynomial need to pass through
        Args:
            t (numpy array): array or list containing nodes.
        """
        t = np.asarray(t)
        dim = len(t)
        VM = np.zeros([dim, dim])
        VM[:, 0] = 1
        for i in range(1, dim):
            VM[:, i] = (t[:] - t[(i - 1)]) * VM[:, i - 1]

        return VM

    def Horner_newton(self, weights, xi, x):
        """
        Horner scheme to evaluate polynomials based on newton basis
        Args:
            weights (numpy array): Quadrature weights
            xi (numpy array): Quadrature nodes
            x (numpy array): Points to evalute polynomial on
        """
        y = np.zeros_like(x)
        for i in range(len(weights)):
            y = y * (x - xi[(-i - 1)]) + weights[(-i - 1)]

        return y

    def gauss_legendre(self, n, b, A=-1, B=1):
        """
        Genrates nodes and weights for gauss legendre quadrature
        Args:
            n (int): Number of quadrature nodes
            b (real): End of quadrature domain (start is a=0)
        """
        a = 0
        poly = legendre(n)
        polyd = poly.deriv()
        nodes = poly.roots
        nodes = np.sort(nodes)
        weights = 2/((1-nodes**2)*(np.polyval(polyd, nodes))**2)
        gl_nodes = ((b - a) * nodes + a * B - b * A) / (B - A)
        gl_weights = (b-a)/(B-A)*weights
        return gl_nodes, gl_weights

    def get_weights(self, b):
        """
        Genrates integration weights my integrating Lagrande polynomials to the 
        quadrature nodes.
        Args:
            b (real): End of quadrature domain (start is a=0)
        """
        M = self.M
        nodes_m, weights_m = self.gauss_legendre(np.ceil(M/2), b)  # use gauss-legendre quadrature to integrate polynomials
        weights = np.zeros(M)
        for j in np.arange(M):
            coeff = np.zeros(M)
            coeff[j] = 1.0  # is unity because it needs to be scaled with y_j for interpolation we have  sum y_j*l_j
            poly_coeffs = scipy.linalg.solve_triangular(self.NewtonVM(self.nodes), coeff, lower=True)
            eval_newt_poly = self.Horner_newton(poly_coeffs, self.nodes, nodes_m)
            weights[j] = np.dot(weights_m, eval_newt_poly)
        return weights

    def Qmatrix(self):
        """
        Integration Matrix
        """
        M = self.M
        self.Q = np.zeros([M, M])

        # for all nodes, get weights for the interval [tleft,node]
        for m in np.arange(M):
            w = self.get_weights(self.nodes[m])
            self.Q[m, 0:] = w

    def Qfinal(self):
        """
        Final Update Integration Vector
        """
        M = self.M
        self.Qfin = np.zeros(M)

        # Get weights for the interval [0,dt]
        w = self.get_weights(self.dt_coarse)
        
        self.Qfin[:] = w

    def Smatrix(self):
        """
        Integration matrix based on Q: sum(S@vector) returns integration
        """
        from copy import deepcopy
        M = self.M
        self.S = np.zeros([M, M])

        self.S[0, :] = deepcopy(self.Q[0, :])
        for m in np.arange(1, M):
            self.S[m, :] = self.Q[m, :] - self.Q[m - 1, :]

    def compute_quad(self):
        """
        Computes integration of F(y) on quadrature nodes
        """
        for j in range(self.M):
            self.quad[j].assign(0.)
            for k in range(self.M):
                self.quad[j] += float(self.S[j, k])*self.fUnodes[k]
    
    def compute_quad_final(self):
        """
        Computes final integration of F(y) on quadrature nodes
        """
        self.quad_final.assign(0.)
        for k in range(self.M):
            self.quad_final += float(self.Qfin[k])*self.fUnodes[k]

    @abstractmethod
    def apply(self, x_out, x_in):
        """
        Apply the SDC time discretisation to advance one whole time step.

        Args:
            x_out (:class:`Function`): the output field to be computed.
            x_in (:class:`Function`): the input field.
        """
        pass

class FE_SDC(SDC):

    def __init__(self, base_scheme, domain, M, maxk, quadrature, field_name=None,
                 linear_solver_parameters=None, nonlinear_solver_parameters=None, final_update=True):
        """
        Initialise Forward Euler SDC scheme
        Args:
            base_scheme (:class:`TimeDiscretisation`): Base time stepping scheme to get first guess of solution on 
                quadrature nodes.
            domain (:class:`Domain`): the model's domain object, containing the
                mesh and the compatible function spaces.
            M (int): Number of quadrature nodes to compute spectral integration over
            maxk (int): Max number of correction interations
            quadrature (str): Type of quadrature to be used. Options are gauss-legendre,
                gauss-radau and gauss-lobotto.
            field_name (str, optional): name of the field to be evolved.
                Defaults to None.
            linear_solver_parameters (dict, optional): dictionary of parameters to
                pass to the underlying linear solver. Defaults to None.
            nonlinear_solver_parameters (dict, optional): dictionary of parameters to
                pass to the underlying nonlinear solver. Defaults to None.
            final_update (bool, optional): Whether to compute final update, or just take last
                quadrature value. Defaults to True
        """
        super().__init__(domain, M, maxk, quadrature, field_name=field_name,
                         linear_solver_parameters=linear_solver_parameters,
                         nonlinear_solver_parameters=nonlinear_solver_parameters,
                         final_update=final_update)
        self.base = base_scheme

    def setup(self, equation, apply_bcs=True, *active_labels):
        """
        Set up the Forward Euler SDC time discretisation based on the equation.

        Args:
            equation (:class:`PrognosticEquation`): the model's equation.
            *active_labels (:class:`Label`): labels indicating which terms of
                the equation to include.
        """
        super().setup(equation, apply_bcs, *active_labels)

    @cached_property
    def res_rhs(self):
        """Set up the residual for the calculation of F(Y)."""
        return super(SDC, self).res_rhs
    
    @cached_property
    def res_SDC(self):
        """Set up the residual for the SDC solve."""
        return super(SDC, self).res_SDC

    @cached_property
    def res_fin(self):
        """Set up the residual for final solve."""
        return super(SDC, self).res_fin

    @cached_property
    def solver_fin(self):
        """Set up the problem and the solver for final update."""
        return super(SDC, self).solver_fin

    @cached_property
    def solver_SDC(self):
        """Set up the problem and the solver for SDC solve."""
        # setup linear solver using SDC residual defined in derived class
        prob_SDC = NonlinearVariationalProblem(self.res_SDC, self.U_SDC, bcs=self.bcs)
        solver_name = self.field_name+self.__class__.__name__+"_SDC"
        return NonlinearVariationalSolver(prob_SDC, solver_parameters=self.linear_solver_parameters,
                                          options_prefix=solver_name)

    @cached_property
    def solver_rhs(self):
        """Set up the problem and the solver for mass matrix inversion."""
        return super(SDC, self).solver_rhs

    def apply(self, x_out, x_in):
        self.Un.assign(x_in)
        
        # Compute initial guess on quadrature nodes with low-order
        # base timestepper
        self.Unodes[0].assign(self.Un)
        for m in range(self.M):
            self.base.dt = float(self.dtau[m])
            self.base.apply(self.Unodes[m+1], self.Unodes[m])

        # Iterate through correction sweeps
        k = 0
        while k < self.maxk:
            k += 1
            
            # Compute sum(j=1,M) s_mj*F(y_m^k) 
            for m in range(1, self.M+1):
                self.Uin.assign(self.Unodes[m])
                self.solver_rhs.solve()
                self.fUnodes[m-1].assign(self.Urhs)
            self.compute_quad()
            
            # Loop through quadrature nodes and solve
            self.Unodes1[0].assign(self.Unodes[0])
            for m in range(1, self.M+1):
                # Set dt and other variables for solver
                self.dt = float(self.dtau[m-1])
                self.U0.assign(self.Unodes[m-1])
                self.Un.assign(self.Unodes1[m-1])
                self.Q_.assign(self.quad[m-1])

                # Set initial guess for solver
                self.U_SDC.assign(self.Unodes[m])

                # Compute 
                # y_m^(k+1) = y_(m-1)^(k+1) + dtau_m*(F(y_(m-1)^(k+1)) - F(y_(m-1)^k))
                #             + sum(j=1,M) s_mj*F(y^k)
                self.solver_SDC.solve(self.Unodes[m-1])
                self.Unodes1[m].assign(self.U_SDC)
            for m in range(1, self.M+1):
                self.Unodes[m].assign(self.Unodes1[m])

            self.Un.assign(self.Unodes1[-1])
        if self.maxk > 0:
            # Compute value at dt rather than final quadrature node dtau_M
            if self.final_update:
                for m in range(1, self.M+1):
                    self.Uin.assign(self.Unodes1[m])
                    self.solver_rhs.solve()
                    self.fUnodes[m-1].assign(self.Urhs)
                self.Un.assign(x_in)
                self.compute_quad_final()
                # Compute y^(n+1) = y^n + sum(j=1,M) q_j*F(y_j)
                self.solver_fin.solve()
                x_out.assign(self.U_fin)
            else:
                # Take value at final quadrature node dtau_M
                x_out.assign(self.Unodes1[-1])
        else:
            x_out.assign(self.Unodes[-1])

class BE_SDC(SDC):

    def __init__(self, base_scheme, domain, M, maxk, quadrature, field_name=None,
                 linear_solver_parameters=None, nonlinear_solver_parameters=None, final_update=True):
        """
        Initialise Backward Euler SDC scheme
        Args:
            base_scheme (:class:`TimeDiscretisation`): Base time stepping scheme to get first guess of solution on 
                quadrature nodes.
            domain (:class:`Domain`): the model's domain object, containing the
                mesh and the compatible function spaces.
            M (int): Number of quadrature nodes to compute spectral integration over
            maxk (int): Max number of correction interations
            quadrature (str): Type of quadrature to be used. Options are gauss-legendre,
                gauss-radau and gauss-lobotto.
            field_name (str, optional): name of the field to be evolved.
                Defaults to None.
            linear_solver_parameters (dict, optional): dictionary of parameters to
                pass to the underlying linear solver. Defaults to None.
            nonlinear_solver_parameters (dict, optional): dictionary of parameters to
                pass to the underlying nonlinear solver. Defaults to None.
            final_update (bool, optional): Whether to compute final update, or just take last
                quadrature value. Defaults to True
        """
        super().__init__(domain, M, maxk, quadrature, field_name=field_name,
                         linear_solver_parameters=linear_solver_parameters,
                         nonlinear_solver_parameters=nonlinear_solver_parameters,
                         final_update=final_update)
        self.base = base_scheme

    def setup(self, equation, apply_bcs=True, *active_labels):
        """
        Set up the Forward Euler SDC time discretisation based on the equation.

        Args:
            equation (:class:`PrognosticEquation`): the model's equation.
            *active_labels (:class:`Label`): labels indicating which terms of
                the equation to include.
        """
        super().setup(equation, apply_bcs, *active_labels)
    
    @cached_property
    def res_rhs(self):
        """Set up the residual for the calculation of F(Y)."""
        return super(SDC, self).res_rhs
    
    @cached_property
    def res_fin(self):
        """Set up the residual for final solve."""
        return super(SDC, self).res_fin

    
    @property
    def res_SDC(self):
        F = self.residual.label_map(lambda t: t.has_label(time_derivative),
                                    map_if_false=lambda t: self.dt*t)
        # y_m^(k+1) + dt*F(y_m^(k+1))
        F_imp = F.label_map(all_terms,
                            replace_subject(self.U_SDC, old_idx=self.idx))
        # y_(m-1)^(k+1)
        F_exp = F.label_map(all_terms, replace_subject(self.Un, old_idx=self.idx))
        F_exp = F_exp.label_map(lambda t: t.has_label(time_derivative),
                                lambda t: -1*t,
                                drop)
        # dt*F(y_m^k)
        F01 = F.label_map(lambda t: t.has_label(time_derivative),
                            drop,
                            replace_subject(self.U01, old_idx=self.idx))

        F01 = F01.label_map(all_terms, lambda t: -1*t)

        # sum(j=1,M) s_mj*F(y_m^k)
        Q = self.residual.label_map(lambda t: t.has_label(time_derivative),
                                    replace_subject(self.Q_, old_idx=self.idx),
                                    drop)

        F_SDC = F_imp + F_exp + F01  + Q
        return F_SDC.form
    
    @cached_property
    def solver_fin(self):
        """Set up the problem and the solver for final update."""
        return super(SDC, self).solver_fin

    @cached_property
    def solver_SDC(self):
        """Set up the problem and the solver for SDC solve."""
        return super(SDC, self).solver_SDC

    @cached_property
    def solver_rhs(self):
        """Set up the problem and the solver for mass matrix inversion."""
        return super(SDC, self).solver_rhs

    def apply(self, x_out, x_in):
        self.Un.assign(x_in)

        # Compute initial guess on quadrature nodes with low-order
        # base timestepper
        self.Unodes[0].assign(self.Un)
        for m in range(self.M):
            self.base.dt = self.dtau[m]
            self.base.apply(self.Unodes[m+1], self.Unodes[m])
        
        # Iterate through correction sweeps
        k = 0
        while k < self.maxk:
            k += 1
            
            # Compute sum(j=1,M) s_mj*F(y_m^k)
            for m in range(1, self.M+1):
                self.Uin.assign(self.Unodes[m])
                self.solver_rhs.solve()
                self.fUnodes[m-1].assign(self.Urhs)
            self.compute_quad()

            # Loop through quadrature nodes and solve
            self.Unodes1[0].assign(self.Unodes[0])
            for m in range(1, self.M+1):
                # Set dt and other variables for solver
                self.dt = float(self.dtau[m-1])
                self.U01.assign(self.Unodes[m])
                self.U0.assign(self.Unodes[m-1])
                self.Un.assign(self.Unodes1[m-1])
                self.Q_.assign(self.quad[m-1])
                
                # Set initial guess for solver
                self.U_SDC.assign(self.Unodes[m])

                # Compute 
                # y_m^(k+1) = y_(m-1)^(k+1) + dtau_m*(F(y_(m)^(k+1)) - F(y_(m)^k))
                #             + sum(j=1,M) s_mj*F(y_m^k)
                self.solver_SDC.solve()
                self.Unodes1[m].assign(self.U_SDC)
            for m in range(1, self.M+1):
                self.Unodes[m].assign(self.Unodes1[m])

            self.Un.assign(self.Unodes1[-1])
        if self.maxk > 0:
            # Compute value at dt rather than final quadrature node dtau_M
            if self.final_update:
                for m in range(1, self.M+1):
                    self.Uin.assign(self.Unodes1[m])
                    self.solver_rhs.solve()
                    self.fUnodes[m-1].assign(self.Urhs)
                self.Un.assign(x_in)
                self.compute_quad_final()
                # Compute y^(n+1) = y^n + sum(j=1,M) q_j*F(y_j)
                self.solver_fin.solve()
                x_out.assign(self.U_fin)
            else:
                # Take value at final quadrature node dtau_M
                x_out.assign(self.Unodes1[-1])
        else:
            x_out.assign(self.Unodes[-1])

class IMEX_SDC(SDC):

    def __init__(self, base_scheme, domain, M, maxk, quadrature, field_name=None,
                 linear_solver_parameters=None, nonlinear_solver_parameters=None, final_update=True):
        """
        Initialise IMEX (FWSW) Euler SDC scheme
        Args:
            base_scheme (:class:`TimeDiscretisation`): Base time stepping scheme to get first guess of solution on 
                quadrature nodes.
            domain (:class:`Domain`): the model's domain object, containing the
                mesh and the compatible function spaces.
            M (int): Number of quadrature nodes to compute spectral integration over
            maxk (int): Max number of correction interations
            quadrature (str): Type of quadrature to be used. Options are gauss-legendre,
                gauss-radau and gauss-lobotto.
            field_name (str, optional): name of the field to be evolved.
                Defaults to None.
            linear_solver_parameters (dict, optional): dictionary of parameters to
                pass to the underlying linear solver. Defaults to None.
            nonlinear_solver_parameters (dict, optional): dictionary of parameters to
                pass to the underlying nonlinear solver. Defaults to None.
            final_update (bool, optional): Whether to compute final update, or just take last
                quadrature value. Defaults to True
        """
        super().__init__(domain, M, maxk, quadrature, field_name=field_name,
                         linear_solver_parameters=linear_solver_parameters,
                         nonlinear_solver_parameters=nonlinear_solver_parameters,
                         final_update=final_update)
        self.base = base_scheme

    def setup(self, equation, apply_bcs=True, *active_labels):
        """
        Set up the Forward Euler SDC time discretisation based on the equation.

        Args:
            equation (:class:`PrognosticEquation`): the model's equation.
            *active_labels (:class:`Label`): labels indicating which terms of
                the equation to include.
        """
        super().setup(equation, apply_bcs, *active_labels)

    @cached_property
    def res_rhs(self):
        """Set up the residual for the calculation of F(Y)."""
        return super(SDC, self).res_rhs

    @cached_property
    def res_fin(self):
        """Set up the residual for final solve."""
        return super(SDC, self).res_fin

    
    @property
    def res_SDC(self):
        F = self.residual.label_map(lambda t: t.has_label(time_derivative),
                                    map_if_false=lambda t: self.dt*t)

        # y_m^(k+1) + dt*F(y_m^(k+1))
        F_imp = F.label_map(lambda t: any(t.has_label(time_derivative, implicit)),
                            replace_subject(self.U_SDC, old_idx=self.idx),
                            drop)
        # y_(m-1)^(k+1) + dt*S(y_(m-1)^(k+1))
        F_exp = F.label_map(lambda t: any(t.has_label(time_derivative, explicit)),
                            replace_subject(self.Un, old_idx=self.idx),
                            drop)
        F_exp = F_exp.label_map(lambda t: t.has_label(time_derivative),
                                lambda t: -1*t)

        # dt*F(y_m^k)
        F01 = F.label_map(lambda t: t.has_label(implicit),
                          replace_subject(self.U01, old_idx=self.idx),
                          drop)

        F01 = F01.label_map(all_terms, lambda t: -1*t)

        # dt*S(y_(m-1)^k)
        F0 = F.label_map(lambda t: t.has_label(explicit),
                         replace_subject(self.U0, old_idx=self.idx),
                         drop)
        F0 = F0.label_map(all_terms, lambda t: -1*t)

        # sum(j=1,M) (s_mj*F(y_m^k) +  s_mj*S(y_m^k))
        Q = self.residual.label_map(lambda t: t.has_label(time_derivative),
                                    replace_subject(self.Q_, old_idx=self.idx),
                                    drop)

        F_SDC = F_imp + F_exp + F01 + F0 + Q
        return F_SDC.form
    
    @cached_property
    def solver_fin(self):
        """Set up the problem and the solver for final update."""
        return super(SDC, self).solver_fin

    @cached_property
    def solver_SDC(self):
        """Set up the problem and the solver for SDC solve."""
        return super(SDC, self).solver_SDC

    @cached_property
    def solver_rhs(self):
        """Set up the problem and the solver for mass matrix inversion."""
        return super(SDC, self).solver_rhs

    def apply(self, x_out, x_in):
        self.Un.assign(x_in)

        # Compute initial guess on quadrature nodes with low-order
        # base timestepper
        self.Unodes[0].assign(self.Un)
        for m in range(self.M):
            self.base.dt = float(self.dtau[m])
            self.base.apply(self.Unodes[m+1], self.Unodes[m])

        # Iterate through correction sweeps
        k = 0
        while k < self.maxk:
            k += 1

            # Compute sum(j=1,M) (s_mj*F(y_m^k) +  s_mj*S(y_m^k))
            for m in range(1, self.M+1):
                self.Uin.assign(self.Unodes[m])
                self.solver_rhs.solve()
                self.fUnodes[m-1].assign(self.Urhs)
            self.compute_quad()

            # Loop through quadrature nodes and solve
            self.Unodes1[0].assign(self.Unodes[0])
            for m in range(1, self.M+1):
                # Set dt and other variables for solver
                self.dt = float(self.dtau[m-1])
                self.U0.assign(self.Unodes[m-1])
                self.U01.assign(self.Unodes[m])
                self.Un.assign(self.Unodes1[m-1])
                self.Q_.assign(self.quad[m-1])

                # Set initial guess for solver
                self.U_SDC.assign(self.Unodes[m])

                # Compute 
                # y_m^(k+1) = y_(m-1)^(k+1) + dtau_m*(F(y_(m)^(k+1)) - F(y_(m)^k)
                #             + S(y_(m-1)^(k+1)) - S(y_(m-1)^k))
                #             + sum(j=1,M) s_mj*(F+S)(y^k)
                self.solver_SDC.solve()
                self.Unodes1[m].assign(self.U_SDC)
            for m in range(1, self.M+1):
                self.Unodes[m].assign(self.Unodes1[m])

            self.Un.assign(self.Unodes1[-1])
        if self.maxk > 0:
            # Compute value at dt rather than final quadrature node dtau_M
            if self.final_update:
                for m in range(1, self.M+1):
                    self.Uin.assign(self.Unodes1[m])
                    self.solver_rhs.solve()
                    self.fUnodes[m-1].assign(self.Urhs)
                self.Un.assign(x_in)
                self.compute_quad_final()
                # Compute y^(n+1) = y^n + sum(j=1,M) q_j*F(y_j)
                self.solver_fin.solve()
                x_out.assign(self.U_fin)
            else:
                # Take value at final quadrature node dtau_M
                x_out.assign(self.Un)
        else:
            x_out.assign(self.Unodes[-1])