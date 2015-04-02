import numpy as np
from scipy.stats import ks_2samp

from .interruptible_pool import Pool
from .clustered_kde import optimized_kde


class GetLnProbWrapper(object):
    def __init__(self, lnpost, kde):
        self.lnpost = lnpost
        self.kde = kde

    def __call__(self, p):
        lnpost = self.lnpost(p)
        kde = self.kde(p)

        return np.array([lnpost, kde])


class Sampler(object):
    """
    An Ensemble sampler.

    The :attr:`chain` member of this object has the shape:
    ``(nsteps, nwalkers, dim)`` where ``nsteps`` is the number of steps

    :param nwalkers:
        The number of individual MCMC chains to include in the ensemble.

    :param dim:
        Number of dimensions in the parameter space.

    :param lnpostfn:
        A function that takes a vector in the parameter space as input and
        returns the natural logarithm of the posterior probability for that
        position.

    """
    def __init__(self, nwalkers, ndim, lnpostfn,
                 processes=None, pool=None):
        self.nwalkers = nwalkers
        self.dim = ndim

        self._get_lnpost = lnpostfn

        self.iterations = 0

        self._kde = None

        self.pool = pool
        self.processes = processes
        if self.processes != 1 and self.pool is None:
            self.pool = Pool(self.processes)

        self._chain = np.empty((0, self.nwalkers, self.dim))
        self._lnpost = np.empty((0, self.nwalkers))
        self._lnprop = np.empty((0, self.nwalkers))
        self._acceptance = np.zeros((0, self.nwalkers))

        self._failed_p = None

    def burnin(self, p0, lnpost0=None, lnprop0=None,
               update_interval=10, max_steps=None):
        """
        Use two-sample K-S tests to determine when burnin is complete.  The
        interval over which distributions are compared will be adapted based
        on the average acceptance rate of the walkers.

        :param p0:
            A list of the initial walker positions.  It should have the
            shape ``(nwalkers, dim)``.

        :param lnpost0: (optional)
            The list of log posterior probabilities for the walkers at
            positions ``p0``. If ``lnpost0 is None``, the initial
            values are calculated. It should have the shape
            ``(nwalkers, dim)``.

        :param lnprop0: (optional)
            The list of log proposal densities for the walkers at
            positions ``p0``. If ``lnprop0 is None``, the initial
            values are calculated. It should have the shape
            ``(nwalkers, dim)``.

        :param update_interval: (optional)
            The number of steps between proposal updates.

        :param max_steps: (optional)
            An absolute maximum number of steps to take, in case burnin
            is too painful.

        After burning in, this method returns:

        * ``p`` - A list of the current walker positions, the shape of which
            will be ``(nwalkers, dim)``.

        * ``lnpost`` - The list of log posterior probabilities for the
          walkers at positions ``p``, with shape ``(nwalkers, dim)``.

        * ``lnprop`` - The list of log proposal densities for the
          walkers at positions ``p``, with shape ``(nwalkers, dim)``.

        """
        p0 = np.array(p0)

        # Go until all two-sample K-S p-values are above this
        critical_pval = 0.05

        # Start the K-S testing interval at the update interval length
        test_interval = update_interval

        # Determine the maximum iteration to look for
        start = self.iterations
        max_iter = np.inf
        if max_steps is not None:
            max_iter = start + max_steps

            # If max_steps < update interval, at least run to max_steps
            test_interval = min(test_interval, max_steps)

        burned_in = False
        while not burned_in:
            # Give up if we're about to exceed the maximum number of iterations
            if self.iterations + test_interval > max_iter:
                break

            p, lnpost, lnprop = self.sample(p0, lnpost0, lnprop0,
                                            test_interval, update_interval)

            burned_in = True
            for par in range(self.dim):
                KS, pval = ks_2samp(p0[:, par], p[:, par])

                if pval < critical_pval:
                    burned_in = False
                    break

            # Adjust the interval so >~ 90% of walkers accept a jump
            if not burned_in:
                # Use the average acceptance of the last step to window
                #   over the last 10 accepted jumps (on average)
                window = int(10 * 1/np.mean(self.acceptance[-1]))
                acceptance_rates = np.mean(self.acceptance[-window:], axis=0)

                # Use the first decile of the walkers' acceptance rates to
                #  decide the next test interval
                index = int(.1*self.nwalkers)
                low_rate = np.sort(acceptance_rates)[index]

                # If there is a lot of variance in acceptance rates, get
                #   another 10 acceptances (on average) and check again.
                if low_rate > 0.:
                    test_interval = int(1./low_rate)
                else:
                    test_interval = window

                p0, lnpost0, lnprop0 = p, lnpost, lnprop

        if not burned_in:
            print "Burnin unsuccessful."

        return (p, lnpost, lnprop)

    def sample(self, p0=None, lnpost0=None, lnprop0=None,
               iterations=1, update_interval=10):
        """
        Advance the ensemble ``iterations`` steps.

        :param p0 (optional):
            A list of the initial walker positions of shape
            ``(nwalkers, dim)``.  If ``None`` and a proposal distribution
            exists, walker positions will be drawn from the proposal.

        :param lnpost0: (optional)
            The list of log posterior probabilities for the walkers at
            positions ``p0``. If ``lnpost0 is None``, the initial
            values are calculated. It should have the shape
            ``(nwalkers, dim)``.

        :param lnprop0: (optional)
            The list of log proposal densities for the walkers at
            positions ``p0``. If ``lnprop0 is None``, the initial
            values are calculated. It should have the shape
            ``(nwalkers, dim)``.

        :param iterations: (optional)
            The number of steps to run.

        :param update_interval: (optional)
            The number of steps between proposal updates.

        After ``iteration`` steps, this method returns:

        * ``p`` - A list of the current walker positions, the shape of which
            will be ``(nwalkers, dim)``.

        * ``lnpost`` - The list of log posterior probabilities for the
          walkers at positions ``p``, with shape ``(nwalkers, dim)``.

        * ``lnprop`` - The list of log proposal densities for the
          walkers at positions ``p``, with shape ``(nwalkers, dim)``.

        """
        if p0 is None:
            p = self.draw(self.nwalkers)
        else:
            p = np.array(p0)

        if self.pool is None:
            m = map
        else:
            m = self.pool.map

        # Build a proposal if one doesn'g already exist
        if self._kde is None:
            self._kde = optimized_kde(p, pool=self.pool)

        lnpost = lnpost0
        lnprop = lnprop0

        if lnpost is None or lnprop is None:
            results = np.array(m(GetLnProbWrapper(self._get_lnpost,
                                                  self._kde), p))
            lnpost = results[:, 0] if lnpost is None else lnpost
            lnprop = results[:, 1] if lnprop is None else lnprop

        # Prepare arrays for storage ahead of time
        self._chain = np.concatenate(
            (self._chain, np.zeros((iterations, self.nwalkers, self.dim))))
        self._lnpost = np.concatenate(
            (self._lnpost, np.zeros((iterations, self.nwalkers))))
        self._lnprop = np.concatenate(
            (self._lnprop, np.zeros((iterations, self.nwalkers))))
        self._acceptance = np.concatenate(
            (self._acceptance, np.zeros((iterations, self.nwalkers))))

        for i in xrange(int(iterations)):
            try:
                # Draw new walker locations from the proposal
                p_p = self.draw(self.nwalkers)

                # Calculate the posterior probability and proposal density
                # at the proposed locations
                try:
                    results = np.array(m(GetLnProbWrapper(self._get_lnpost,
                                                          self._kde), p_p))
                    lnpost_p = results[:, 0]
                    lnprop_p = results[:, 1]

                # Catch any exceptions and exit gracefully
                except Exception as e:
                    self.rollback(self.iterations)
                    self._failed_p = p_p

                    print "Offending samples stored in ``failed_p``."
                    raise

                # Calculate the (ln) Metropolis-Hastings ration
                ln_mh_ratio = lnpost_p - lnpost + lnprop - lnprop_p

                # Accept if ratio is greater than 1
                acc = ln_mh_ratio > 0

                # Decide which of the remainder will be accepted
                worse = ~acc
                nworse = np.sum(worse)
                uniform_draws = np.random.rand(nworse)
                acc[worse] = ln_mh_ratio[worse] > np.log(uniform_draws)

                # Update locations and probability densities
                if np.any(acc):
                    p[acc] = p_p[acc]
                    lnpost[acc] = lnpost_p[acc]
                    lnprop[acc] = lnprop_p[acc]

                # Store stuff
                self._chain[self.iterations, :, :] = p
                self._lnpost[self.iterations, :] = lnpost
                self._lnprop[self.iterations, :] = lnprop
                self._acceptance[self.iterations, :] = acc

                self.iterations += 1

                # Update the proposal at the requested interval
                if self.iterations % update_interval == 0:
                    self._kde = optimized_kde(p, pool=self.pool)

            except KeyboardInterrupt:
                self.rollback(self.iterations)
                raise

        return (p, lnpost, lnprop)

    def draw(self, N):
        """
        Draw ``N`` samples from the current proposal distribution.
        """
        return self._kde.draw(N)

    @property
    def failed_p(self):
        """
        Sample that caused the last exception.
        """
        return self._failed_p

    @property
    def chain(self):
        """
        Ensemble's past samples,
            with shape ``(iterations, nwalkers, ndim)``.
        """
        return self._chain

    @property
    def lnpost(self):
        """
        Ensemble's past posterior probabilities,
            with shape ``(iterations, nwalkers)``.
        """
        return self._lnpost

    @property
    def lnprop(self):
        """
        Ensemble's past proposal probabilities,
            with shape ``(iterations, nwalkers)``.
        """
        return self._lnprop

    @property
    def acceptance(self):
        """
        Boolean array of ensemble's past acceptances,
            with shape ``(iterations, nwalkers)``.
        """
        return self._acceptance

    def rollback(self, iteration):
        """
        Shrink arrays down to a length of ``iteration`` and reset the
        pool if there is one.
        """
        self._chain = self._chain[:iteration]
        self._lnpost = self._lnpost[:iteration]
        self._lnprop = self._lnprop[:iteration]
        self._acceptance = self._acceptance[:iteration]

        # Close the old pool and open a new one
        if self.processes != 1:
            self.pool.close()
            self.pool = Pool(self.processes)
