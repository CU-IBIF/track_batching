function [tracksFinal, kalmanInfoLink, errFlag] = trackCloseGapsKalmanSparse(movieInfo, costMatrices, gapCloseParam, kalmanFunctions, probDim, saveResults, verbose)
% Test stub: one track per feature row, straight through all frames.
assert(isstruct(costMatrices) && numel(costMatrices) == 2);
assert(isequal(size(costMatrices(2).parameters.brownStdMult), [gapCloseParam.timeWindow 1]));
assert(ischar(kalmanFunctions.initialize));
n = numel(movieInfo);
nTr = size(movieInfo(1).xCoord, 1);
for k = 1:nTr
    M = nan(1, 8 * n);
    for t = 1:n
        M((t - 1) * 8 + (1:8)) = [movieInfo(t).xCoord(k, 1) movieInfo(t).yCoord(k, 1) 0 movieInfo(t).amp(k, 1) 0.1 0.1 0 1];
    end
    if k == 2, M(9:16) = nan; end  % a gap
    tracksFinal(k, 1).tracksFeatIndxCG = ones(1, n);
    tracksFinal(k, 1).tracksCoordAmpCG = M;
    tracksFinal(k, 1).seqOfEvents = [1 1 1 NaN; n 2 1 NaN];
end
kalmanInfoLink = []; errFlag = 0;
save([saveResults.dir filesep saveResults.filename], 'costMatrices', 'gapCloseParam', 'kalmanFunctions', 'tracksFinal');
end
